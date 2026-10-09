// AvaPermissionsHelper — the macOS desktop-automation daemon.
//
// This binary is the one process that holds the machine's TCC grants (Screen
// Recording, Accessibility). It MUST be launched by launchd so it is its own
// "responsible process" — a binary forked from a terminal session inherits
// the terminal's TCC identity and can only borrow its grants, which is fragile
// (breaks under SSH, breaks on restart). Launched by launchd it has a stable,
// own identity; signed with a stable certificate that identity survives every
// rebuild, so the operator grants permission exactly once.
//
// Synthetic HID events and accessibility-tree reads require this process's
// Accessibility grant. macOS silently drops synthetic events without it, so
// dispatch refuses those calls explicitly instead of reporting a success for
// work that never reached the desktop.
//
// It serves a line-delimited JSON request/response protocol over a Unix socket,
// mirroring the shared-daemon pattern used elsewhere in the system. Calls are
// served serially (one connection fully handled before the next is accepted):
// the work is GUI automation against a single desktop, which is inherently
// serial. Child spawning, keeper callbacks and SIGCHLD handling also run on
// dispatch queues; their shared native ownership is guarded separately below.
//
//   Request:  {"id": 1, "method": "ping"}
//             {"id": 2, "method": "screencapture_region", "x":0,"y":0,"w":800,"h":600,"path":"/tmp/x.png"}
//   Response: {"id": 1, "ok": true,  "result": {...}}
//             {"id": 2, "ok": false, "error": "message"}

import AppKit
import CoreGraphics
import Darwin
import Foundation
import ScreenCaptureKit

// MARK: - JSON line IO

/// Read one '\n'-delimited line from a connected socket fd. Returns nil on EOF.
func readLine(_ fd: Int32) -> Data? {
    var buf = Data()
    var byte = [UInt8](repeating: 0, count: 1)
    while true {
        let n = read(fd, &byte, 1)
        if n == 0 { return buf.isEmpty ? nil : buf }  // EOF
        if n < 0 { return nil }
        if byte[0] == 0x0A { return buf }  // '\n'
        buf.append(byte[0])
    }
}

/// Write a JSON object followed by '\n' to a connected socket fd. If `obj` is not
/// serializable (e.g. a non-finite float slipped in), write a guaranteed-safe
/// error line instead of nothing -- a silent no-write would leave the client
/// blocked in recv() forever.
func writeJSONLine(_ fd: Int32, _ obj: [String: Any]) {
    var data: Data
    if let d = try? JSONSerialization.data(withJSONObject: obj) {
        data = d
    } else {
        data = Data(#"{"id":null,"ok":false,"error":"response not serializable"}"#.utf8)
    }
    data.append(0x0A)
    data.withUnsafeBytes { raw in
        var off = 0
        let base = raw.bindMemory(to: UInt8.self).baseAddress!
        while off < data.count {
            let n = write(fd, base + off, data.count - off)
            if n <= 0 { return }
            off += n
        }
    }
}

// MARK: - Native operations

enum OpError: Error { case bad(String) }

private let maxFileReadBytes: Int64 = 32 * 1024 * 1024

/// Skip the first-run permission-list registration when set to "1". A test
/// affordance, not a runtime switch: the two-section chain smoke boots a
/// throwaway-signed helper in a live Aqua session, where a fresh code identity
/// would otherwise raise Screen Recording / Accessibility dialogs on a
/// machine nobody is sitting at. Production plists never set it, and it only
/// skips the System Settings registration nudge — every enforcement point is
/// untouched (default behavior is byte-for-byte the old one).
private let skipRegistrationEnvironment = "AVA_PERMISSIONS_HELPER_SKIP_REGISTRATION"

struct Child {
    let pid: pid_t
    let startedAt: Date
}

// One ownership boundary covers native spawn/publication, signals, and waitpid
// itself. An exited direct child retains its PID until waitpid reaps it, so a
// signal under this lock cannot race reaping and target a reused foreign PID.
// RootKeeper shares this non-recursive lock; its *Locked methods never acquire it.
private let childOwnershipLock = NSLock()
private var children: [String: Child] = [:]
private var helperStopping = false
private var childReaper: DispatchSourceSignal?

func withChildOwnership<T>(_ body: () throws -> T) rethrows -> T {
    childOwnershipLock.lock()
    defer { childOwnershipLock.unlock() }
    return try body()
}

func childIsAlive(_ pid: pid_t) -> Bool {
    kill(pid, 0) == 0
}

func reapExitedChildren() {
    withChildOwnership {
        rootKeeper.reapExitedChildLocked()
        // Do not reap Foundation-owned Process children (e.g. screen capture).
        // Each owner consumes only the direct children it actually published.
        for (name, child) in children {
            var status: Int32 = 0
            if waitpid(child.pid, &status, WNOHANG) == child.pid {
                children.removeValue(forKey: name)
            }
        }
    }
}

func startChildReaper() {
    let source = DispatchSource.makeSignalSource(signal: SIGCHLD)
    source.setEventHandler(handler: reapExitedChildren)
    source.resume()
    childReaper = source
}

func setCloseOnExec(_ fd: Int32, _ enabled: Bool) -> Bool {
    let flags = fcntl(fd, F_GETFD)
    guard flags >= 0 else { return false }
    let updated = enabled ? flags | FD_CLOEXEC : flags & ~FD_CLOEXEC
    return fcntl(fd, F_SETFD, updated) == 0
}

func reportResponsiblePID() {
    typealias ProcPIDInfo = @convention(c) (
        pid_t, Int32, UInt64, UnsafeMutableRawPointer?, Int32
    ) -> Int32
    typealias ResponsiblePID = @convention(c) (pid_t) -> pid_t

    let symbols = UnsafeMutableRawPointer(bitPattern: -2)  // RTLD_DEFAULT on Darwin.
    guard let procPIDInfoSymbol = dlsym(symbols, "proc_pidinfo") else {
        FileHandle.standardError.write(Data("AvaPermissionsHelper: responsible-probe unsupported\n".utf8))
        return
    }

    let ownPID = getpid()
    var responsiblePID: pid_t = 0
    let procPIDResponsible: Int32 = 2
    let procPIDInfo = unsafeBitCast(procPIDInfoSymbol, to: ProcPIDInfo.self)
    let bytes = withUnsafeMutablePointer(to: &responsiblePID) { pointer in
        procPIDInfo(
            ownPID,
            procPIDResponsible,
            0,
            UnsafeMutableRawPointer(pointer),
            Int32(MemoryLayout<pid_t>.size)
        )
    }

    // Older SDKs expose flavor 2 as PROC_PIDTASKALLINFO. Keep the startup probe
    // useful there through the long-standing private responsibility symbol.
    if bytes != MemoryLayout<pid_t>.size || responsiblePID <= 0,
       let fallbackSymbol = dlsym(symbols, "responsibility_get_pid_responsible_for_pid") {
        let responsiblePIDForProcess = unsafeBitCast(fallbackSymbol, to: ResponsiblePID.self)
        responsiblePID = responsiblePIDForProcess(ownPID)
    }
    guard responsiblePID > 0 else {
        FileHandle.standardError.write(Data("AvaPermissionsHelper: responsible-probe unsupported\n".utf8))
        return
    }
    FileHandle.standardError.write(
        Data("AvaPermissionsHelper: responsible_pid=\(responsiblePID) self=\(ownPID)\n".utf8)
    )
}

func createParentDirectory(of path: String) throws {
    let directory = URL(fileURLWithPath: path).deletingLastPathComponent()
    do {
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true
        )
    } catch {
        throw OpError.bad("could not create output directory: \(directory.path)")
    }
}

/// Spawn one direct child without an intermediate process. PR-3 will verify
/// posix_spawn's launchd ResponsiblePid inheritance through tccd
/// AUTHREQ_ATTRIBUTION; if it does not inherit, a fallback will be evaluated.
func spawnProcess(_ req: [String: Any]) throws -> [String: Any] {
    guard let name = req["name"] as? String, !name.isEmpty,
          let argv = req["argv"] as? [String], !argv.isEmpty,
          (argv[0] as NSString).isAbsolutePath,
          let env = req["env"] as? [String: String],
          let cwd = req["cwd"] as? String,
          let stdoutPath = req["stdout"] as? String,
          (stdoutPath as NSString).isAbsolutePath,
          let stderrPath = req["stderr"] as? String,
          (stderrPath as NSString).isAbsolutePath
    else {
        throw OpError.bad(
            "spawn needs non-empty name, absolute argv[0]/stdout/stderr, argv, env, cwd"
        )
    }
    return try withChildOwnership {
        guard !helperStopping else { throw OpError.bad("helper retirement has closed admission") }
        if let child = children[name], childIsAlive(child.pid) {
            return ["pid": child.pid, "reused": true]
        }
        let spawnedPID = try spawnDetachedChild(
            argv: argv, environment: env, cwd: cwd,
            stdoutPath: stdoutPath, stderrPath: stderrPath
        )
        children[name] = Child(pid: spawnedPID, startedAt: Date())
        return ["pid": spawnedPID, "reused": false]
    }
}

/// The shared low-level spawn the session table and the root keeper both use:
/// one detached child (SETSID | CLOEXEC_DEFAULT, stdio redirected to
/// append-only files, cwd set) with no bookkeeping. `environment` must be the
/// child's full environment; `AVA_PERMISSIONS_HELPER_PID` is stamped here.
func spawnDetachedChild(
    argv: [String], environment: [String: String], cwd: String,
    stdoutPath: String, stderrPath: String
) throws -> pid_t {
    try createParentDirectory(of: stdoutPath)
    try createParentDirectory(of: stderrPath)

    var argumentPointers = argv.map { strdup($0) }
    guard argumentPointers.allSatisfy({ $0 != nil }) else {
        argumentPointers.forEach { if let pointer = $0 { free(pointer) } }
        throw OpError.bad("could not allocate argv")
    }
    argumentPointers.append(nil)
    defer { argumentPointers.forEach { if let pointer = $0 { free(pointer) } } }

    var childEnvironment = environment
    childEnvironment["AVA_PERMISSIONS_HELPER_PID"] = String(getpid())
    var environmentPointers = childEnvironment.map { key, value in strdup("\(key)=\(value)") }
    guard environmentPointers.allSatisfy({ $0 != nil }) else {
        environmentPointers.forEach { if let pointer = $0 { free(pointer) } }
        throw OpError.bad("could not allocate envp")
    }
    environmentPointers.append(nil)
    defer { environmentPointers.forEach { if let pointer = $0 { free(pointer) } } }

    var fileActions: posix_spawn_file_actions_t?
    var attributes: posix_spawnattr_t?
    var result = posix_spawn_file_actions_init(&fileActions)
    guard result == 0 else {
        throw OpError.bad("posix_spawn file actions init failed: \(String(cString: strerror(result)))")
    }
    defer { posix_spawn_file_actions_destroy(&fileActions) }
    result = posix_spawnattr_init(&attributes)
    guard result == 0 else {
        throw OpError.bad("posix_spawn attributes init failed: \(String(cString: strerror(result)))")
    }
    defer { posix_spawnattr_destroy(&attributes) }

    // Reset the child's signal state explicitly: children inherit the calling
    // thread's signal mask and the process's dispositions through posix_spawn,
    // and the root keeper spawns from a GCD worker thread, where libdispatch
    // blocks most signals including SIGTERM — without this reset a
    // keeper-spawned ava-root never sees SIGTERM (empirically confirmed
    // 2026-09-12; same fix family as services/agent_runner/pty_sessions/session.py's SIG_DFL
    // reset before spawning a shell).
    let spawnFlags = Int16(
        POSIX_SPAWN_SETSID | POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF
            | POSIX_SPAWN_SETSIGMASK
    )
    var signalsToDefault = sigset_t()
    sigemptyset(&signalsToDefault)
    for signum in [SIGHUP, SIGINT, SIGTERM, SIGPIPE] {
        sigaddset(&signalsToDefault, signum)
    }
    var emptySignalMask = sigset_t()
    sigemptyset(&emptySignalMask)
    let setupResults = [
        posix_spawnattr_setflags(&attributes, spawnFlags),
        posix_spawnattr_setsigdefault(&attributes, &signalsToDefault),
        posix_spawnattr_setsigmask(&attributes, &emptySignalMask),
        posix_spawn_file_actions_addopen(
            &fileActions, STDIN_FILENO, "/dev/null", O_RDONLY, mode_t(0)
        ),
        posix_spawn_file_actions_addopen(
            &fileActions, STDOUT_FILENO, stdoutPath,
            O_WRONLY | O_CREAT | O_APPEND, mode_t(0o644)
        ),
        posix_spawn_file_actions_addopen(
            &fileActions, STDERR_FILENO, stderrPath,
            O_WRONLY | O_CREAT | O_APPEND, mode_t(0o644)
        ),
        posix_spawn_file_actions_addchdir_np(&fileActions, cwd),
    ]
    if let failure = setupResults.first(where: { $0 != 0 }) {
        throw OpError.bad("posix_spawn setup failed: \(String(cString: strerror(failure)))")
    }

    var spawnedPID: pid_t = 0
    let spawnResult = argumentPointers.withUnsafeMutableBufferPointer { argvBuffer in
        environmentPointers.withUnsafeMutableBufferPointer { envBuffer in
            posix_spawn(
                &spawnedPID, argvBuffer[0], &fileActions, &attributes,
                argvBuffer.baseAddress, envBuffer.baseAddress
            )
        }
    }
    guard spawnResult == 0 else {
        throw OpError.bad("posix_spawn failed: \(String(cString: strerror(spawnResult)))")
    }
    return spawnedPID
}

func sessionList(_ req: [String: Any]) throws -> [String: Any] {
    let prefix: String
    if let supplied = req["prefix"] {
        guard let supplied = supplied as? String else {
            throw OpError.bad("session_list prefix must be a string")
        }
        prefix = supplied
    } else {
        prefix = ""
    }
    let sessions: [[String: Any]] = withChildOwnership {
        children
            .filter { $0.key.hasPrefix(prefix) }
            .sorted { $0.key < $1.key }
            .map { name, child in
                ["name": name, "pid": child.pid, "alive": childIsAlive(child.pid)]
            }
    }
    return ["sessions": sessions]
}

func sessionHas(_ req: [String: Any]) throws -> [String: Any] {
    guard let name = req["name"] as? String else {
        throw OpError.bad("session_has needs string name")
    }
    let alive = withChildOwnership { children[name].map { childIsAlive($0.pid) } ?? false }
    return ["alive": alive]
}

func signalSession(_ req: [String: Any]) throws -> [String: Any] {
    guard let signalNumber = req["sig"] as? Int,
          let signalValue = Int32(exactly: signalNumber), signalValue > 0
    else { throw OpError.bad("signal needs positive int sig") }
    let name = req["name"] as? String
    let requestedPID = req["pid"] as? Int
    guard (name == nil) != (requestedPID == nil) else {
        throw OpError.bad("signal needs exactly one of name or pid")
    }

    if let name {
        return try withChildOwnership {
            guard let child = children[name] else {
                throw OpError.bad("unknown session: \(name)")
            }
            return ["sent": kill(child.pid, signalValue) == 0]
        }
    }
    guard let requestedPID, requestedPID > 0, let exactPID = pid_t(exactly: requestedPID) else {
        throw OpError.bad("signal pid must be positive")
    }
    return ["sent": kill(exactPID, signalValue) == 0]
}

/// Resolve an allowed path or reject it. A bare prefix is insufficient:
/// `/Users/ava/DownloadsEvil` is not in `/Users/ava/Downloads`.
// SECURITY SYNC: tests/components/services/test_permissions_helper.py::
// _is_whitelisted_file_path mirrors this exact resolved-path boundary rule in
// `resolvedWhitelistedFilePath`. Update both implementations together whenever
// whitelist containment changes.
func resolvedWhitelistedFilePath(_ requestedPath: String) throws -> String {
    guard requestedPath.hasPrefix("/") else { throw OpError.bad("outside whitelist") }

    let resolvedPath = ((requestedPath as NSString).standardizingPath as NSString)
        .resolvingSymlinksInPath
    let home = NSHomeDirectory()
    let roots = ["Downloads", "Desktop", ".ava/incoming"].map { relativePath in
        (((home as NSString).appendingPathComponent(relativePath) as NSString)
            .standardizingPath as NSString)
            .resolvingSymlinksInPath
    }
    let allowed = roots.contains { root in
        resolvedPath == root || resolvedPath.hasPrefix(root + "/")
    }
    guard allowed else { throw OpError.bad("outside whitelist") }
    return resolvedPath
}

/// List immediate children of a whitelisted directory with the metadata the
/// Python client exposes. Names are sorted before metadata is collected so the
/// reply order is stable.
func fileList(_ req: [String: Any]) throws -> [String: Any] {
    guard let requestedPath = req["path"] as? String else {
        throw OpError.bad("file_list needs string path")
    }
    let path = try resolvedWhitelistedFilePath(requestedPath)
    let fm = FileManager.default
    var isDirectory = ObjCBool(false)
    guard fm.fileExists(atPath: path, isDirectory: &isDirectory) else {
        throw OpError.bad("not found")
    }
    guard isDirectory.boolValue else { throw OpError.bad("not a directory") }

    let names: [String]
    do {
        names = try fm.contentsOfDirectory(atPath: path).sorted()
    } catch {
        throw OpError.bad("not found")
    }
    let entries = try names.map { name -> [String: Any] in
        let attributes = try fm.attributesOfItem(
            atPath: (path as NSString).appendingPathComponent(name)
        )
        guard let size = attributes[.size] as? NSNumber,
              let modificationDate = attributes[.modificationDate] as? Date
        else { throw OpError.bad("not found") }
        return [
            "name": name,
            "size": size.int64Value,
            "mtime": Int64(modificationDate.timeIntervalSince1970),
            "is_dir": (attributes[.type] as? FileAttributeType) == .typeDirectory,
        ]
    }
    return ["entries": entries]
}

/// Read a bounded regular file from a whitelisted location as base64.
func fileRead(_ req: [String: Any]) throws -> [String: Any] {
    guard let requestedPath = req["path"] as? String else {
        throw OpError.bad("file_read needs string path")
    }
    let path = try resolvedWhitelistedFilePath(requestedPath)
    let fm = FileManager.default
    var isDirectory = ObjCBool(false)
    guard fm.fileExists(atPath: path, isDirectory: &isDirectory) else {
        throw OpError.bad("not found")
    }
    guard !isDirectory.boolValue,
          let attributes = try? fm.attributesOfItem(atPath: path),
          (attributes[.type] as? FileAttributeType) == .typeRegular
    else { throw OpError.bad("not a regular file") }
    guard let size = attributes[.size] as? NSNumber else { throw OpError.bad("not found") }
    guard size.int64Value <= maxFileReadBytes else { throw OpError.bad("file too large") }

    guard let content = try? Data(contentsOf: URL(fileURLWithPath: path)) else {
        throw OpError.bad("not found")
    }
    guard content.count <= maxFileReadBytes else { throw OpError.bad("file too large") }
    return ["content_b64": content.base64EncodedString()]
}

/// Capture a screen region to a PNG file via the system screencapture tool.
/// screencapture runs as a child of this launchd-parented process, so it
/// inherits this binary's Screen Recording grant.
func screencaptureRegion(_ req: [String: Any]) throws -> [String: Any] {
    guard let x = req["x"] as? Int, let y = req["y"] as? Int,
          let w = req["w"] as? Int, let h = req["h"] as? Int,
          let path = req["path"] as? String
    else { throw OpError.bad("screencapture_region needs int x,y,w,h and string path") }

    guard CGPreflightScreenCaptureAccess() else {
        throw OpError.bad("Screen Recording grant missing for region capture")
    }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    p.arguments = ["-x", "-R\(x),\(y),\(w),\(h)", path]
    try p.run()
    p.waitUntilExit()
    if p.terminationStatus != 0 {
        throw OpError.bad("screencapture exited \(p.terminationStatus)")
    }
    let size = (try? FileManager.default.attributesOfItem(atPath: path)[.size] as? Int) ?? nil
    return ["path": path, "bytes": size ?? -1]
}

/// Let AppKit process bounded queued updates before reading running-app state.
private func refreshAppKit() {
    _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.01))
}

private func selectedRunningApp(_ req: [String: Any]) throws -> NSRunningApplication {
    refreshAppKit()
    let matches: [NSRunningApplication]
    if req["pid"] != nil && req["bundle_id"] == nil {
        let pid = try inputInteger(req, "pid", lower: 1, upper: Int(Int32.max))
        matches = NSWorkspace.shared.runningApplications.filter { Int($0.processIdentifier) == pid }
    } else if req["bundle_id"] != nil && req["pid"] == nil {
        guard let bundle = req["bundle_id"] as? String, !bundle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { throw OpError.bad("bundle_id must be a nonempty string") }
        matches = NSWorkspace.shared.runningApplications.filter { $0.bundleIdentifier == bundle }
    } else { throw OpError.bad("app target needs exactly pid or bundle_id") }
    guard matches.count == 1 else { throw OpError.bad("app target is missing or ambiguous") }
    guard !matches[0].isTerminated else { throw OpError.bad("app target has terminated") }
    return matches[0]
}

func listApps() -> [String: Any] {
    refreshAppKit()
    let apps: [[String: Any]] = NSWorkspace.shared.runningApplications.filter { !$0.isTerminated }.map {
        ["pid": Int($0.processIdentifier), "name": $0.localizedName as Any? ?? NSNull(),
         "bundle_id": $0.bundleIdentifier as Any? ?? NSNull()]
    }
    return ["apps": apps]
}

func listWindows(_ req: [String: Any]) throws -> [String: Any] {
    guard CGPreflightScreenCaptureAccess() else {
        throw OpError.bad("Screen Recording grant missing: window inventory metadata is unavailable")
    }
    var selectedPid: pid_t?
    if let raw = req["app"] {
        guard let selector = raw as? String, !selector.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { throw OpError.bad("app must be a nonempty name or bundle_id") }
        refreshAppKit()
        let matches = NSWorkspace.shared.runningApplications.filter {
            !$0.isTerminated && ($0.localizedName == selector || $0.bundleIdentifier == selector)
        }
        guard matches.count == 1 else { throw OpError.bad("app selector is missing or ambiguous") }
        selectedPid = matches[0].processIdentifier
    }
    guard let inventory = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]]
    else { throw OpError.bad("window inventory is unavailable in this login session") }
    var windows: [[String: Any]] = []
    for window in inventory {
        guard (window[kCGWindowLayer as String] as? Int) == 0,
              let pid = window[kCGWindowOwnerPID as String] as? Int,
              let id = window[kCGWindowNumber as String] as? Int,
              let rawBounds = window[kCGWindowBounds as String] as? [String: Any],
              let bounds = CGRect(dictionaryRepresentation: rawBounds as CFDictionary),
              bounds.width > 0, bounds.height > 0
        else { continue }
        if let chosen = selectedPid, pid != Int(chosen) { continue }
        windows.append(["window_id": id, "pid": pid,
            "owner": window[kCGWindowOwnerName as String] ?? NSNull(),
            "title": window[kCGWindowName as String] ?? NSNull(),
            "x": bounds.minX, "y": bounds.minY, "w": bounds.width, "h": bounds.height,
            "on_screen": window[kCGWindowIsOnscreen as String] as? Bool ?? false])
    }
    return ["windows": windows]
}

/// Explicit foreground activation; confirm the actual focused application's PID through AX.
func focusApp(_ req: [String: Any]) throws -> [String: Any] {
    let app = try selectedRunningApp(req)
    guard app.activate(options: []) else { throw OpError.bad("app activation was refused") }
    let system = AXUIElementCreateSystemWide()
    AXUIElementSetMessagingTimeout(system, 0.2)
    let deadline = Date(timeIntervalSinceNow: 1)
    repeat {
        refreshAppKit()
        var focused: CFTypeRef?
        if AXUIElementCopyAttributeValue(system, kAXFocusedApplicationAttribute as CFString, &focused) == .success,
           let focused = focused, CFGetTypeID(focused) == AXUIElementGetTypeID() {
            var pid: pid_t = 0
            if AXUIElementGetPid(focused as! AXUIElement, &pid) == .success, pid == app.processIdentifier {
                return ["focused": true, "pid": Int(pid)]
            }
        }
    } while Date() < deadline
    throw OpError.bad("app activation was requested but focused PID could not be confirmed; capture again")
}

/// Pump the main run loop while ScreenCaptureKit completes its asynchronous callback.
private func captureCallback<T>(_ start: (@escaping (T?, Error?) -> Void) -> Void) throws -> T {
    let lock = NSLock()
    var finished = false
    var result: T?
    var failure: Error?
    start { value, error in
        lock.lock()
        result = value; failure = error; finished = true
        lock.unlock()
    }
    let deadline = Date(timeIntervalSinceNow: 5)
    while Date() < deadline {
        lock.lock()
        let done = finished
        let value = result
        let error = failure
        lock.unlock()
        if done {
            if let error = error { throw OpError.bad("window capture failed: \(error.localizedDescription)") }
            guard let value = value else { throw OpError.bad("window capture returned no image or content") }
            return value
        }
        _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.01))
    }
    throw OpError.bad("window capture timed out")
}

@available(macOS 14.0, *)
private func capturedWindow(_ req: [String: Any]) throws -> [String: Any] {
    let pid = try inputInteger(req, "pid", lower: 1, upper: Int(Int32.max))
    let id = try inputInteger(req, "window_id", lower: 1, upper: Int(UInt32.max))
    guard let path = req["path"] as? String else { throw OpError.bad("window capture needs string path") }
    guard CGPreflightScreenCaptureAccess() else { throw OpError.bad("Screen Recording grant missing for window capture") }
    let content: SCShareableContent = try captureCallback { completion in
        SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: false, completionHandler: completion)
    }
    guard let window = content.windows.first(where: {
        Int($0.windowID) == id && Int($0.owningApplication?.processID ?? 0) == pid
    }) else { throw OpError.bad("window target is stale or does not belong to the requested PID") }
    let filter = SCContentFilter(desktopIndependentWindow: window)
    let rect = filter.contentRect
    let scale = Double(filter.pointPixelScale)
    guard rect.width > 0, rect.height > 0, scale.isFinite, scale > 0 else {
        throw OpError.bad("window target has no capturable geometry")
    }
    let configuration = SCStreamConfiguration()
    configuration.width = Int((rect.width * scale).rounded())
    configuration.height = Int((rect.height * scale).rounded())
    configuration.showsCursor = false
    configuration.ignoreShadowsSingleWindow = true
    let image: CGImage = try captureCallback { completion in
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration, completionHandler: completion)
    }
    // Re-resolve ownership and geometry after capture; moving/recycled windows require a new observation.
    let latest: SCShareableContent = try captureCallback { completion in
        SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: false, completionHandler: completion)
    }
    guard let fresh = latest.windows.first(where: { Int($0.windowID) == id }),
          Int(fresh.owningApplication?.processID ?? 0) == pid, fresh.frame == window.frame
    else { throw OpError.bad("window identity or geometry changed during capture") }
    let bitmap = NSBitmapImageRep(cgImage: image)
    guard let png = bitmap.representation(using: .png, properties: [:]) else { throw OpError.bad("could not encode window PNG") }
    try png.write(to: URL(fileURLWithPath: path), options: .atomic)
    return ["path": path, "bytes": png.count, "width": image.width, "height": image.height,
            "origin": ["x": rect.minX, "y": rect.minY], "scale": scale]
}

func screencaptureWindow(_ req: [String: Any]) throws -> [String: Any] {
    guard #available(macOS 14.0, *) else { throw OpError.bad("window capture requires macOS 14 or later") }
    return try capturedWindow(req)
}

/// Coerce a JSON number (which may decode as Int or Double) to Double.
func numericDouble(_ v: Any?) -> Double? {
    if let d = v as? Double { return d }
    if let i = v as? Int { return Double(i) }
    return nil
}

// Accessibility grant gate. Synthetic HID events posted to .cghidEventTap are
// silently dropped by macOS unless this process holds the Accessibility grant,
// so the helper refuses those calls loudly instead of returning a success that
// never happened. The authorization prompt can only be answered by a human in
// System Settings (and is not shown at all in a launchd background session),
// so the helper never waits on it: ask at most once per window, fail the call.
var lastAXPromptAt = Date.distantPast
let axPromptMinInterval: TimeInterval = 30.0
let axGrantError = "Accessibility grant missing (ax_trusted=false): macOS drops synthetic " +
    "click/type/key events from a process without this grant, so the action did not run. " +
    "The authorization prompt was triggered; enable AvaPermissionsHelper in System Settings " +
    "> Privacy & Security > Accessibility, then retry. Rebuilding or re-signing the helper " +
    "resets this grant once."

func axTrustedOrPrompt() -> Bool {
    if AXIsProcessTrusted() { return true }
    let now = Date()
    if now.timeIntervalSince(lastAXPromptAt) >= axPromptMinInterval {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)
        lastAXPromptAt = now
    }
    return false
}

/// Strict JSON input primitives shared by the atomic input operations.
func inputNumber(_ req: [String: Any], _ key: String, defaultValue: Double? = nil) throws -> Double {
    if req[key] == nil, let value = defaultValue { return value }
    guard let number = req[key] as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite
    else { throw OpError.bad("\(key) must be a finite number") }
    return number.doubleValue
}

func inputInteger(_ req: [String: Any], _ key: String, lower: Int, upper: Int,
                  defaultValue: Int? = nil) throws -> Int {
    if req[key] == nil, let value = defaultValue { return value }
    guard let number = req[key] as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(),
          !["f", "d"].contains(String(cString: number.objCType)),
          let value = Int(exactly: number.doubleValue), value >= lower, value <= upper
    else { throw OpError.bad("\(key) must be an integer in \(lower)...\(upper)") }
    return value
}

func inputBool(_ req: [String: Any], _ key: String) throws -> Bool {
    if req[key] == nil { return false }
    guard let number = req[key] as? NSNumber,
          CFGetTypeID(number) == CFBooleanGetTypeID()
    else { throw OpError.bad("\(key) must be a boolean") }
    return number.boolValue
}

func inputDuration(_ req: [String: Any], maximum: Double) throws -> Double {
    let duration = try inputNumber(req, "duration_ms", defaultValue: 0)
    guard duration >= 0 && duration <= maximum
    else { throw OpError.bad("duration_ms must be in 0...\(maximum)") }
    return duration / 1000
}

func inputModifiers(_ req: [String: Any], legacyCmd: Bool = false) throws -> CGEventFlags {
    var names: [String] = []
    if let raw = req["modifiers"] {
        guard let values = raw as? [String], values.count <= 4, Set(values).count == values.count
        else { throw OpError.bad("modifiers must be a list of distinct shift/ctrl/alt/cmd names") }
        names = values
    }
    var flags: CGEventFlags = legacyCmd ? .maskCommand : []
    for name in names {
        switch name {
        case "shift": flags.insert(.maskShift)
        case "ctrl": flags.insert(.maskControl)
        case "alt": flags.insert(.maskAlternate)
        case "cmd": flags.insert(.maskCommand)
        default: throw OpError.bad("unknown modifier: \(name)")
        }
    }
    return flags
}

func inputMouseEvent(_ kind: CGEventType, _ point: CGPoint, _ button: CGMouseButton,
                     _ flags: CGEventFlags, count: Int64 = 0) throws -> CGEvent {
    guard let event = CGEvent(mouseEventSource: nil, mouseType: kind,
                              mouseCursorPosition: point, mouseButton: button)
    else { throw OpError.bad("could not create mouse event") }
    event.flags = flags
    event.setIntegerValueField(.mouseEventClickState, value: count)
    return event
}

func postInputEvent(_ event: CGEvent) {
    // Events are preallocated for safe release, but their timestamps describe posting.
    event.timestamp = DispatchTime.now().uptimeNanoseconds
    event.post(tap: .cghidEventTap)
}

/// Allocate modifier presses and their reverse releases before any input is posted.
func inputModifierPairs(_ flags: CGEventFlags) throws -> [(CGEvent, CGEvent)] {
    let keys: [(CGEventFlags, CGKeyCode)] = [(.maskShift, 56), (.maskControl, 59),
                                           (.maskAlternate, 58), (.maskCommand, 55)]
    var active: CGEventFlags = []
    var pairs: [(CGEvent, CGEvent)] = []
    for (flag, code) in keys where flags.contains(flag) {
        guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
              let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false)
        else { throw OpError.bad("could not create modifier events") }
        up.type = .flagsChanged; up.flags = active
        active.insert(flag)
        down.type = .flagsChanged; down.flags = active
        pairs.append((down, up))
    }
    return pairs
}

func pressModifiers(_ pairs: [(CGEvent, CGEvent)]) {
    for pair in pairs { postInputEvent(pair.0) }
}

func releaseModifiers(_ pairs: [(CGEvent, CGEvent)]) {
    for pair in pairs.reversed() { postInputEvent(pair.1) }
}

/// Post a synthetic mouse click at a global screen coordinate (move + down + up).
/// Pass "double": true for a second click. Dispatch refuses it without the
/// Accessibility grant instead of posting events macOS would silently drop.
func click(_ req: [String: Any]) throws -> [String: Any] {
    let x = try inputNumber(req, "x"), y = try inputNumber(req, "y")
    let double = try inputBool(req, "double")
    let count = try inputInteger(req, "click_count", lower: 1, upper: 3, defaultValue: double ? 2 : 1)
    if double && count != 2 { throw OpError.bad("double=true requires click_count=2") }
    let duration = try inputDuration(req, maximum: 5000)
    let flags = try inputModifiers(req)
    let name: String
    if let raw = req["button"] {
        guard let value = raw as? String else { throw OpError.bad("button must be left/right/middle") }
        name = value
    } else { name = "left" }
    let button: CGMouseButton, downKind: CGEventType, upKind: CGEventType
    switch name {
    case "left": button = .left; downKind = .leftMouseDown; upKind = .leftMouseUp
    case "right": button = .right; downKind = .rightMouseDown; upKind = .rightMouseUp
    case "middle": button = .center; downKind = .otherMouseDown; upKind = .otherMouseUp
    default: throw OpError.bad("unknown mouse button: \(name)")
    }
    let point = CGPoint(x: x, y: y)
    let move = try inputMouseEvent(.mouseMoved, point, button, flags)
    let pairs = try (1...count).map { index in
        (try inputMouseEvent(downKind, point, button, flags, count: Int64(index)),
         try inputMouseEvent(upKind, point, button, flags, count: Int64(index)))
    }
    let modifiers = try inputModifierPairs(flags)
    func once(_ pair: (CGEvent, CGEvent)) {
        postInputEvent(pair.0)
        defer { postInputEvent(pair.1) }
        if duration > 0 { Thread.sleep(forTimeInterval: duration) }
    }
    pressModifiers(modifiers)
    defer { releaseModifiers(modifiers) }
    postInputEvent(move)
    for (index, pair) in pairs.enumerated() {
        once(pair)
        if index + 1 < pairs.count { Thread.sleep(forTimeInterval: 0.075) }
    }
    return ["clicked": ["x": x, "y": y], "double": count == 2, "button": name, "click_count": count]
}

/// A bounded straight-line drag. Prepare every event before pressing, so allocation
/// failures cannot leave the button down. The release remains local to this call
/// even if the requesting socket disconnects while the synchronous action runs.
func drag(_ req: [String: Any]) throws -> [String: Any] {
    func coordinate(_ key: String) throws -> Double {
        guard let number = req[key] as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite
        else { throw OpError.bad("drag needs finite numeric \(key)") }
        return number.doubleValue
    }
    let sx = try coordinate("start_x"), sy = try coordinate("start_y")
    let ex = try coordinate("end_x"), ey = try coordinate("end_y")
    func event(_ kind: CGEventType, _ x: Double, _ y: Double) throws -> CGEvent {
        guard let ev = CGEvent(mouseEventSource: nil, mouseType: kind,
                               mouseCursorPosition: CGPoint(x: x, y: y), mouseButton: .left)
        else { throw OpError.bad("could not create drag mouse event") }
        ev.flags = []
        return ev
    }
    let move = try event(.mouseMoved, sx, sy)
    let down = try event(.leftMouseDown, sx, sy)
    let up = try event(.leftMouseUp, ex, ey)
    let steps = 12
    let movements = try (1...steps).map { step in
        let fraction = Double(step) / Double(steps)
        return try event(.leftMouseDragged,
                         sx * (1 - fraction) + ex * fraction,
                         sy * (1 - fraction) + ey * fraction)
    }
    move.post(tap: .cghidEventTap)
    down.post(tap: .cghidEventTap)
    defer { up.post(tap: .cghidEventTap) }
    for movement in movements {
        Thread.sleep(forTimeInterval: 0.01)
        movement.post(tap: .cghidEventTap)
    }
    return ["start": ["x": sx, "y": sy], "end": ["x": ex, "y": ey]]
}

/// Post a single key down/up by virtual keycode, optionally with Command held.
/// Flags are set explicitly (0 when no modifier) so a plain key after a Cmd+key
/// event cannot inherit a stale Command flag. Dispatch refuses it without the
/// Accessibility grant instead of posting events macOS would silently drop.
func key(_ req: [String: Any]) throws -> [String: Any] {
    let code = try inputInteger(req, "code", lower: 0, upper: 65535)
    let cmd = try inputBool(req, "cmd")
    let flags = try inputModifiers(req, legacyCmd: cmd)
    let duration = try inputDuration(req, maximum: 10000)
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: CGKeyCode(code), keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: CGKeyCode(code), keyDown: false)
    else { throw OpError.bad("could not create key event") }
    down.flags = flags
    up.flags = flags
    let modifier: CGEventFlags
    switch code {
    case 54, 55: modifier = .maskCommand
    case 56, 60: modifier = .maskShift
    case 58, 61: modifier = .maskAlternate
    case 59, 62: modifier = .maskControl
    default: modifier = []
    }
    if !modifier.isEmpty {
        down.type = .flagsChanged; up.type = .flagsChanged
        down.flags.insert(modifier)
        up.flags.subtract(modifier)
    }
    let modifiers = try inputModifierPairs(flags.subtracting(modifier))
    pressModifiers(modifiers)
    defer { releaseModifiers(modifiers) }
    postInputEvent(down)
    defer { postInputEvent(up) }
    if duration > 0 { Thread.sleep(forTimeInterval: duration) }
    return ["key": code, "cmd": flags.contains(.maskCommand)]
}

/// Move the cursor to (x, y) then post a vertical scroll of `dy` pixels (negative
/// scrolls toward older content). Dispatch refuses it without the Accessibility
/// grant instead of posting events macOS would silently drop.
func scroll(_ req: [String: Any]) throws -> [String: Any] {
    let x = try inputNumber(req, "x"), y = try inputNumber(req, "y")
    let dy = try inputInteger(req, "dy", lower: Int(Int32.min), upper: Int(Int32.max))
    let dx = try inputInteger(req, "dx", lower: Int(Int32.min), upper: Int(Int32.max), defaultValue: 0)
    let flags = try inputModifiers(req)
    let move = try inputMouseEvent(.mouseMoved, CGPoint(x: x, y: y), .left, flags)
    guard let event = CGEvent(scrollWheelEvent2Source: nil, units: .pixel,
                             wheelCount: 2, wheel1: Int32(dy), wheel2: Int32(dx), wheel3: 0)
    else { throw OpError.bad("could not create scroll event") }
    event.flags = flags
    let modifiers = try inputModifierPairs(flags)
    pressModifiers(modifiers)
    defer { releaseModifiers(modifiers) }
    postInputEvent(move)
    postInputEvent(event)
    return ["scrolled": dy, "dx": dx]
}

func mouseMove(_ req: [String: Any]) throws -> [String: Any] {
    let x = try inputNumber(req, "x"), y = try inputNumber(req, "y")
    let flags = try inputModifiers(req)
    let event = try inputMouseEvent(.mouseMoved, CGPoint(x: x, y: y), .left, flags)
    let modifiers = try inputModifierPairs(flags)
    pressModifiers(modifiers)
    defer { releaseModifiers(modifiers) }
    postInputEvent(event)
    return ["moved": ["x": x, "y": y]]
}

func cursorPosition() throws -> [String: Any] {
    guard let event = CGEvent(source: nil) else { throw OpError.bad("could not read cursor position") }
    return ["x": event.location.x, "y": event.location.y]
}

/// Report the geometry of an app's normal (layer-0) window via the window-server
/// list. Works even when the accessibility tree is unavailable. Reading other
/// apps' window owner names requires the Screen Recording grant.
func windowInfo(_ req: [String: Any]) throws -> [String: Any] {
    guard let owner = req["owner"] as? String else { throw OpError.bad("window_info needs string owner") }
    let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
    for w in list {
        guard (w[kCGWindowOwnerName as String] as? String) == owner,
              (w[kCGWindowLayer as String] as? Int) == 0,
              let bounds = w[kCGWindowBounds as String] as? [String: Any]
        else { continue }
        var rect = CGRect.zero
        guard CGRectMakeWithDictionaryRepresentation(bounds as CFDictionary, &rect) else { continue }
        if rect.width > 200, rect.height > 200 {
            return ["owner": owner, "x": rect.minX, "y": rect.minY, "w": rect.width, "h": rect.height]
        }
    }
    throw OpError.bad("no normal window for \(owner)")
}

/// Report whether the login session is locked or off-console, so the caller can
/// refuse GUI automation that a locked screen would silently drop.
func sessionInfo() -> [String: Any] {
    let d = (CGSessionCopyCurrentDictionary() as? [String: Any]) ?? [:]
    return [
        "locked": (d["CGSSessionScreenIsLocked"] as? Int) == 1,
        "on_console": (d["kCGSSessionOnConsoleKey"] as? Int) == 1,
    ]
}

/// Report the main display's geometry in LOGICAL points plus the
/// physical<->logical scale factor. Computer-use callers map screenshot pixels
/// (physical) to click coordinates (logical) via `scale`. On a 1x display,
/// physical and logical coordinates match.
///
/// The scale is derived from CoreGraphics' live display mode (pixel size vs
/// point size) instead of `NSScreen.backingScaleFactor`: AppKit caches screen
/// objects per process and only refreshes them on a screen-parameters
/// notification, which a process without an event loop may never receive —
/// the helper reported scale 2 while the live display, the same session's
/// NSScreen, and the captured PNG all said 1x, halving every click
/// (2026-08-30 probe). Measuring from the live display mode avoids the cache.
func screenSize(_ req: [String: Any]) throws -> [String: Any] {
    let id = CGMainDisplayID()
    let bounds = CGDisplayBounds(id)
    let pixelW = CGDisplayPixelsWide(id)
    let scale = (pixelW > 0 && bounds.width > 0)
        ? Double(pixelW) / Double(bounds.width)
        : 1.0
    return [
        "x": Double(bounds.minX), "y": Double(bounds.minY),
        "w": Double(bounds.width), "h": Double(bounds.height),
        "scale": scale,
    ]
}

/// Report the frontmost application's display name, or "" when none is focused.
/// The computer-use gate matches this against its denied-app keywords before
/// letting a click/type/key/scroll through.
func frontmostApp() -> [String: Any] {
    refreshAppKit()
    let app = NSWorkspace.shared.frontmostApplication
    return ["app": app?.localizedName ?? ""]
}

/// Type a UTF-8 string as synthetic keyboard input. Dispatch refuses it without
/// the Accessibility grant instead of posting events macOS would silently drop.
/// Sends the text as a Unicode payload on a single key down/up pair.
func typeText(_ req: [String: Any]) throws -> [String: Any] {
    guard let text = req["text"] as? String else { throw OpError.bad("type needs string text") }
    let units = Array(text.utf16)
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false)
    else { throw OpError.bad("could not create key event") }
    down.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
    up.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
    return ["typed": text.count]
}

/// Report the on-screen geometry of an application's frontmost window via the
/// accessibility tree. Dispatch refuses it without the Accessibility grant
/// instead of making an accessibility-tree request that macOS would deny.
func axWindowInfo(_ req: [String: Any]) throws -> [String: Any] {
    refreshAppKit()
    guard let appName = req["app"] as? String else { throw OpError.bad("ax_window_info needs string app") }
    let running = NSWorkspace.shared.runningApplications.first {
        $0.localizedName == appName || $0.bundleIdentifier == appName
    }
    guard let app = running else { throw OpError.bad("app not running: \(appName)") }
    let axApp = AXUIElementCreateApplication(app.processIdentifier)

    var winRef: CFTypeRef?
    guard AXUIElementCopyAttributeValue(axApp, kAXFocusedWindowAttribute as CFString, &winRef) == .success,
          let win = winRef
    else { throw OpError.bad("no focused window for \(appName) (Accessibility granted?)") }
    let window = win as! AXUIElement

    func axValue(_ attr: String) -> CFTypeRef? {
        var v: CFTypeRef?
        return AXUIElementCopyAttributeValue(window, attr as CFString, &v) == .success ? v : nil
    }
    var pos = CGPoint.zero
    var size = CGSize.zero
    if let pv = axValue(kAXPositionAttribute) { AXValueGetValue(pv as! AXValue, .cgPoint, &pos) }
    if let sv = axValue(kAXSizeAttribute) { AXValueGetValue(sv as! AXValue, .cgSize, &size) }
    for v in [pos.x, pos.y, size.width, size.height] where !v.isFinite {
        throw OpError.bad("window geometry not finite for \(appName)")  // else JSON serialization throws and hangs the client
    }
    return ["app": appName, "x": pos.x, "y": pos.y, "w": size.width, "h": size.height]
}

// MARK: - AX tree

/// Accessibility-tree walk of one application window (`ax_tree`, read-only)
/// and single-element actions on what it returned (`ax_act`).
/// The helper serves one request at a time, so a walk is bounded three ways: a
/// node cap, a depth cap and a total time budget, plus a per-element messaging
/// timeout so one hung target app cannot hold the socket for the system default
/// (about six seconds). Whatever was read when a bound trips is returned with
/// `truncated` / `timed_out` set; filtering, collapsing, rendering and stable
/// ids belong to the Python side, which tells nodes apart across walks by the
/// fingerprint each carries. `ax_act` addresses a node by the raw id of the
/// latest walk and answers `stale` when that element is gone or changed.
private let axTreeAttributes: [String] = [
    kAXRoleAttribute, kAXSubroleAttribute, kAXTitleAttribute, kAXDescriptionAttribute,
    kAXValueAttribute, kAXIdentifierAttribute, kAXPositionAttribute, kAXSizeAttribute,
    kAXEnabledAttribute, kAXFocusedAttribute, kAXSelectedAttribute,
    kAXChildrenAttribute, kAXVisibleChildrenAttribute,
]
private let axVisibleChildrenRoles: Set<String> = [
    "AXList", "AXTable", "AXOutline", "AXBrowser", "AXScrollArea",
]
private let axMaxStringLength = 200
private let axTreeFrameworkMarkers: [(marker: String, name: String)] = [
    ("Electron Framework.framework", "electron"),
    ("Chromium Embedded Framework.framework", "cef"),
    ("Google Chrome Framework.framework", "chromium"),
    ("Microsoft Edge Framework.framework", "chromium"),
    ("Brave Browser Framework.framework", "chromium"),
]

/// Processes whose Chromium accessibility we already switched on during this
/// helper's life: the switch is sticky in the target app until it quits, so a
/// repeat walk neither sets it again nor waits for the tree to appear.
private var axEnabledPids: Set<pid_t> = []

/// A live element reference plus the signature (role, identifier, title,
/// description) it had when it was read; `ax_act` refuses an element whose
/// signature changed since, so a recycled row is never acted on by mistake.
private struct AXEntry {
    let element: AXUIElement
    let sig: String
    let window: AXUIElement
}

/// Raw ids ("1", "2", ...) of the last walks; the client maps them to its own
/// stable ids through each node's fingerprint. An unscoped walk replaces the
/// table, a scoped one adds to it.
private var axElementTable: [Int: AXEntry] = [:]
private var axNextElementID = 1

private func fnv1a(_ text: String) -> String {
    var hash: UInt64 = 0xcbf29ce484222325
    for byte in text.utf8 {
        hash ^= UInt64(byte)
        hash = hash &* 0x100000001b3
    }
    return String(hash, radix: 16)
}

/// The identity text that distinguishes siblings of one role: the explicit
/// identifier, else the title, else the description. Values are excluded
/// (they change as the user types). A window title participates in its fingerprint;
/// a fingerprint alone cannot prove window identity.
private func axDiscriminator(_ values: [AnyObject?]) -> String {
    for index in [5, 2, 3] {
        if let v = axString(values[index]) { return String(v.prefix(60)) }
    }
    return ""
}

private func axSignature(role: String?, values: [AnyObject?]) -> String {
    let parts = [role, axString(values[5]), axString(values[2]), axString(values[3])]
    return fnv1a(parts.map { $0 ?? "" }.joined(separator: "\u{1f}"))
}

private func axBatch(_ element: AXUIElement, _ attributes: [String]) -> [AnyObject?]? {
    var out: CFArray?
    let status = AXUIElementCopyMultipleAttributeValues(
        element, attributes as CFArray, AXCopyMultipleAttributeOptions(rawValue: 0), &out)
    guard status == .success, let values = out as? [AnyObject], values.count == attributes.count
    else { return nil }
    return values.map { value in
        // A missing attribute comes back as an AXValue of type axError.
        if CFGetTypeID(value) == AXValueGetTypeID(),
           AXValueGetType(value as! AXValue) == .axError { return nil }
        return value
    }
}

private func axString(_ value: AnyObject?) -> String? {
    var text: String?
    if let s = value as? String { text = s }
    else if let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() { text = n.stringValue }
    guard let t = text, !t.isEmpty else { return nil }
    return t.count > axMaxStringLength ? String(t.prefix(axMaxStringLength)) : t
}

private func axFlag(_ value: AnyObject?) -> Bool? {
    guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }
    return n.boolValue
}

private func axElements(_ value: AnyObject?) -> [AXUIElement] {
    guard let array = value as? [AnyObject] else { return [] }
    return array.compactMap { item in
        CFGetTypeID(item) == AXUIElementGetTypeID() ? (item as! AXUIElement) : nil
    }
}

private func axFrame(_ position: AnyObject?, _ size: AnyObject?) -> (CGPoint, CGSize)? {
    var pos = CGPoint.zero
    var dim = CGSize.zero
    guard let pv = position, let sv = size,
          CFGetTypeID(pv) == AXValueGetTypeID(), CFGetTypeID(sv) == AXValueGetTypeID(),
          AXValueGetValue(pv as! AXValue, .cgPoint, &pos),
          AXValueGetValue(sv as! AXValue, .cgSize, &dim),
          [pos.x, pos.y, dim.width, dim.height].allSatisfy({ $0.isFinite })  // else JSON serialization throws and hangs the client
    else { return nil }
    return (pos, dim)
}

private func axActionNames(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success, let list = names as? [String]
    else { return [] }
    return list
}

private func axFrameworkHint(_ app: NSRunningApplication) -> String {
    guard let bundle = app.bundleURL else { return "" }
    for entry in axTreeFrameworkMarkers {
        let path = bundle.appendingPathComponent("Contents/Frameworks/" + entry.marker).path
        if FileManager.default.fileExists(atPath: path) { return entry.name }
    }
    // Chromium forks under their own name (Lark's "Lark Framework.framework")
    // keep Chromium's multi-process layout: a framework whose Helpers directory
    // holds a "... Helper (Renderer).app".
    let frameworks = bundle.appendingPathComponent("Contents/Frameworks")
    for entry in (try? FileManager.default.contentsOfDirectory(atPath: frameworks.path)) ?? []
    where entry.hasSuffix(".framework") {
        let helpers = frameworks.appendingPathComponent(entry + "/Helpers").path
        if let inner = try? FileManager.default.contentsOfDirectory(atPath: helpers),
           inner.contains(where: { $0.hasSuffix("Helper (Renderer).app") }) { return "chromium" }
    }
    return ""
}

/// Ask a Chromium-based app (Electron, CEF, Chrome family) to build its
/// accessibility tree, which it otherwise skips unless an assistive tool is
/// attached. `AXManualAccessibility` is the attribute built for exactly this;
/// `AXEnhancedUserInterface` is deliberately not touched (it changes native
/// window behavior and belongs to VoiceOver). The app builds the tree lazily,
/// so the first time per process we wait, bounded, for its window to list
/// children. Returns "set", "already" or "failed".
private func axEnableChromiumAccessibility(_ axApp: AXUIElement, pid: pid_t) -> String {
    if axEnabledPids.contains(pid) { return "already" }
    let status = AXUIElementSetAttributeValue(axApp, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    guard status == .success else { return "failed" }
    axEnabledPids.insert(pid)
    for _ in 0..<10 {
        if let window = axPickWindow(axApp).window {
            var children: CFTypeRef?
            if AXUIElementCopyAttributeValue(window, kAXChildrenAttribute as CFString, &children) == .success,
               let list = children as? [AnyObject], !list.isEmpty { break }
        }
        Thread.sleep(forTimeInterval: 0.1)
    }
    return "set"
}

private func axPickWindow(_ axApp: AXUIElement) -> (window: AXUIElement?, count: Int) {
    var count = 0
    var listRef: CFTypeRef?
    if AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &listRef) == .success {
        count = (listRef as? [AnyObject])?.count ?? 0
    }
    for attribute in [kAXFocusedWindowAttribute, kAXMainWindowAttribute] {
        var ref: CFTypeRef?
        if AXUIElementCopyAttributeValue(axApp, attribute as CFString, &ref) == .success,
           let window = ref, CFGetTypeID(window) == AXUIElementGetTypeID() {
            return ((window as! AXUIElement), count)
        }
    }
    if let first = axElements(listRef as AnyObject?).first { return (first, count) }
    return (nil, count)
}

/// Walk one window (or, with `scope`, the subtree under an id from the last
/// walk) breadth-first and return the raw node list.
func axTree(_ req: [String: Any]) throws -> [String: Any] {
    refreshAppKit()
    guard let appName = req["app"] as? String else { throw OpError.bad("ax_tree needs string app") }
    guard let app = NSWorkspace.shared.runningApplications.first(where: {
        $0.localizedName == appName || $0.bundleIdentifier == appName
    }) else { throw OpError.bad("app not running: \(appName)") }
    let maxNodes = max(1, min(Int(numericDouble(req["max_nodes"]) ?? 600), 2000))
    let maxDepth = max(1, min(Int(numericDouble(req["max_depth"]) ?? 14), 40))
    let budget = max(0.05, min((numericDouble(req["budget_ms"]) ?? 1500) / 1000, 10))
    let messagingTimeout = Float(max(0.05, min((numericDouble(req["timeout_ms"]) ?? 400) / 1000, 5)))

    let axApp = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(axApp, messagingTimeout)
    let framework = axFrameworkHint(app)
    // "n/a": not a Chromium-based app; "off": the caller opted out; "set" /
    // "already" / "failed": see axEnableChromiumAccessibility. Scoped walks
    // read an element that exists, so they never enable anything.
    var enable = "n/a"
    if !framework.isEmpty && req["scope"] == nil {
        enable = (req["enable_ax"] as? Bool ?? true)
            ? axEnableChromiumAccessibility(axApp, pid: app.processIdentifier) : "off"
    }
    let picked = axPickWindow(axApp)
    var meta: [String: Any] = [
        "app": appName, "pid": Int(app.processIdentifier), "windows": picked.count,
        "framework": framework, "ax_enable": enable,
    ]

    let root: AXUIElement
    let originalWindow: AXUIElement
    let scopeFingerprint = req["scope_fp"] as? String
    if let scopeID = numericDouble(req["scope"]).map({ Int($0) }) {
        guard let scopedEntry = axElementTable[scopeID]
        else { throw OpError.bad("unknown scope \(scopeID): the element table was replaced by a later walk") }
        let scoped = scopedEntry.element
        originalWindow = scopedEntry.window
        guard scopeFingerprint != nil else { throw OpError.bad("a scoped ax_tree needs scope_fp") }
        var scopedPid: pid_t = 0
        guard AXUIElementGetPid(scoped, &scopedPid) == .success, scopedPid == app.processIdentifier
        else { throw OpError.bad("scope \(scopeID) does not belong to \(appName)") }
        AXUIElementSetMessagingTimeout(scoped, messagingTimeout)
        root = scoped
    } else {
        axElementTable.removeAll()
        guard let window = picked.window else {
            meta.merge(["nodes": [Any](), "visited": 0, "truncated": false, "timed_out": false,
                        "unreadable": 0, "elapsed_ms": 0]) { $1 }
            return meta
        }
        AXUIElementSetMessagingTimeout(window, messagingTimeout)
        root = window
        originalWindow = window
    }

    let started = Date()
    var nodes: [[String: Any]] = []
    var queue: [(element: AXUIElement, parent: Int?, parentFp: String, depth: Int)] = [(root, nil, "", 0)]
    var siblingCounts: [String: Int] = [:]
    var head = 0
    var truncated = false
    var timedOut = false
    var unreadable = 0
    while head < queue.count {
        let (element, parent, parentFp, depth) = queue[head]
        head += 1
        if nodes.count >= maxNodes { truncated = true; break }
        if Date().timeIntervalSince(started) > budget { timedOut = true; break }
        guard let values = axBatch(element, axTreeAttributes) else { unreadable += 1; continue }
        let role = axString(values[0])
        let id = axNextElementID
        axNextElementID += 1
        axElementTable[id] = AXEntry(element: element, sig: axSignature(role: role, values: values), window: originalWindow)

        // Path fingerprint: parent fingerprint + role + discriminator + the
        // ordinal among same-keyed siblings. It survives value edits and
        // sibling reordering of other kinds, and a scoped walk's root keeps the
        // fingerprint the client already knows it by.
        let fingerprint: String
        if depth == 0, let known = scopeFingerprint {
            fingerprint = known
        } else {
            let segment = (role ?? "") + "|" + axDiscriminator(values)
            let ordinalKey = parentFp + "/" + segment
            let ordinal = siblingCounts[ordinalKey, default: 0]
            siblingCounts[ordinalKey] = ordinal + 1
            fingerprint = fnv1a(ordinalKey + "#" + String(ordinal))
        }

        var node: [String: Any] = ["id": id, "depth": depth, "fp": fingerprint]
        if let parent = parent { node["parent"] = parent }
        if let role = role { node["role"] = role }
        if let v = axString(values[1]) { node["subrole"] = v }
        if let v = axString(values[2]) { node["title"] = v }
        if let v = axString(values[3]) { node["desc"] = v }
        // Secure fields are never echoed, whatever the target app exposes.
        if node["subrole"] as? String != "AXSecureTextField", let v = axString(values[4]) { node["value"] = v }
        if let v = axString(values[5]) { node["ident"] = v }
        if let (pos, dim) = axFrame(values[6], values[7]) {
            node["x"] = Double(pos.x); node["y"] = Double(pos.y)
            node["w"] = Double(dim.width); node["h"] = Double(dim.height)
        }
        if let v = axFlag(values[8]) { node["enabled"] = v }
        if let v = axFlag(values[9]) { node["focused"] = v }
        if let v = axFlag(values[10]) { node["selected"] = v }
        let actions = axActionNames(element)
        if !actions.isEmpty { node["actions"] = actions }

        var children = axElements(values[11])
        if let role = role, axVisibleChildrenRoles.contains(role) {
            let visible = axElements(values[12])
            if !visible.isEmpty { children = visible }
        }
        node["n"] = children.count
        nodes.append(node)
        if depth + 1 >= maxDepth {
            if !children.isEmpty { truncated = true }
            continue
        }
        for child in children { queue.append((child, id, fingerprint, depth + 1)) }
    }
    meta.merge([
        "nodes": nodes, "visited": nodes.count, "truncated": truncated, "timed_out": timedOut,
        "unreadable": unreadable, "elapsed_ms": Int(Date().timeIntervalSince(started) * 1000),
    ]) { $1 }
    return meta
}

// NSString ranges are UTF-16 offsets; do not derive offsets from String.count.
private func axSelectionRange(_ req: [String: Any], value: String) throws -> CFRange {
    guard let text = req["text"] as? String, !text.isEmpty else {
        throw OpError.bad("select_text needs nonempty string text")
    }
    let prefix: String
    if let raw = req["prefix"] {
        guard let string = raw as? String else { throw OpError.bad("prefix must be a string") }
        prefix = string
    } else { prefix = "" }
    let suffix: String
    if let raw = req["suffix"] {
        guard let string = raw as? String else { throw OpError.bad("suffix must be a string") }
        suffix = string
    } else { suffix = "" }
    let selection: String
    if let raw = req["selection_type"] {
        guard let string = raw as? String else { throw OpError.bad("selection_type must be a string") }
        selection = string
    } else { selection = "text" }
    guard ["text", "cursor_before", "cursor_after"].contains(selection) else {
        throw OpError.bad("selection_type must be text, cursor_before or cursor_after")
    }
    guard [text, prefix, suffix].allSatisfy({ $0.unicodeScalars.count <= 10_000 }) else {
        throw OpError.bad("text and context must not exceed 10000 characters")
    }
    let source = value as NSString
    let needleLength = (text as NSString).length
    let prefixLength = (prefix as NSString).length
    let suffixLength = (suffix as NSString).length
    var found: NSRange?
    var cursor = 0
    while cursor <= source.length - needleLength {
        let candidate = source.range(of: text, options: .literal,
            range: NSRange(location: cursor, length: source.length - cursor))
        if candidate.location == NSNotFound { break }
        let end = NSMaxRange(candidate)
        let prefixMatches = candidate.location >= prefixLength && source.compare(prefix, options: .literal,
            range: NSRange(location: candidate.location - prefixLength, length: prefixLength)) == .orderedSame
        let suffixMatches = end + suffixLength <= source.length && source.compare(suffix, options: .literal,
            range: NSRange(location: end, length: suffixLength)) == .orderedSame
        if prefixMatches && suffixMatches {
            guard found == nil else { throw OpError.bad("text selection is ambiguous; add prefix or suffix") }
            found = candidate
        }
        // Advance by one UTF-16 code unit to include overlapping matches.
        cursor = candidate.location + 1
    }
    guard let match = found else { throw OpError.bad("text selection has no exact match") }
    switch selection {
    case "cursor_before": return CFRange(location: match.location, length: 0)
    case "cursor_after": return CFRange(location: NSMaxRange(match), length: 0)
    default: return CFRange(location: match.location, length: match.length)
    }
}


private let axActionTable: [String: String] = [
    "press": kAXPressAction, "show_menu": kAXShowMenuAction,
]

/// Perform one action on an element of the latest walk. Outcomes:
///   stale     -> {"stale": true, "completed": false}: the id is not in the table,
///                or the element no longer answers / no longer has the role,
///                identifier, title and description it had when it was read.
///   completed -> {"completed": true, ...}: the app accepted the action.
///   unanswered-> {"completed": false, "unanswered": true}: the app did not
///                answer within the timeout; the action may still have run.
/// Anything else (no such action, attribute not settable, AX error) throws.
/// The value of `set_value` is written and never echoed or logged.
func axAct(_ req: [String: Any]) throws -> [String: Any] {
    refreshAppKit()
    guard let appName = req["app"] as? String, let action = req["action"] as? String,
          let rawID = numericDouble(req["id"]).map({ Int($0) })
    else { throw OpError.bad("ax_act needs string app, string action and numeric id") }
    guard let app = NSWorkspace.shared.runningApplications.first(where: {
        $0.localizedName == appName || $0.bundleIdentifier == appName
    }) else { throw OpError.bad("app not running: \(appName)") }
    let timeout = Float(max(0.1, min((numericDouble(req["timeout_ms"]) ?? 2000) / 1000, 10)))
    let stale: [String: Any] = ["stale": true, "completed": false]
    guard let entry = axElementTable[rawID] else { return stale }
    var elementPid: pid_t = 0
    guard AXUIElementGetPid(entry.element, &elementPid) == .success, elementPid == app.processIdentifier
    else { return stale }
    AXUIElementSetMessagingTimeout(entry.element, timeout)
    AXUIElementSetMessagingTimeout(entry.window, timeout)
    let axApp = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(axApp, timeout)
    var currentWindows: CFTypeRef?
    guard AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &currentWindows) == .success,
          axElements(currentWindows as AnyObject?).contains(where: { CFEqual($0, entry.window) })
    else { return stale }
    if !CFEqual(entry.element, entry.window) {
        var elementWindow: CFTypeRef?
        guard AXUIElementCopyAttributeValue(entry.element, kAXWindowAttribute as CFString, &elementWindow) == .success,
              let elementWindow = elementWindow, CFEqual(elementWindow, entry.window)
        else { return stale }
    }
    guard let values = axBatch(entry.element, axTreeAttributes) else { return stale }
    let role = axString(values[0])
    guard axSignature(role: role, values: values) == entry.sig else { return stale }

    var status: AXError
    switch action {
    case "press", "show_menu":
        let name = axActionTable[action]!
        let offered = axActionNames(entry.element)
        guard offered.contains(name) else {
            throw OpError.bad("element does not offer \(name) (it offers: \(offered.joined(separator: ", ")))")
        }
        status = AXUIElementPerformAction(entry.element, name as CFString)
    case "perform_action":
        guard let name = req["native_action"] as? String, !name.isEmpty else {
            throw OpError.bad("perform_action needs nonempty string native_action")
        }
        guard axActionNames(entry.element).contains(name) else {
            throw OpError.bad("element does not offer requested native_action")
        }
        status = AXUIElementPerformAction(entry.element, name as CFString)
    case "select_text":
        guard axString(values[1]) != "AXSecureTextField" else {
            throw OpError.bad("text selection is unavailable on secure fields")
        }
        var settable = DarwinBoolean(false)
        guard AXUIElementIsAttributeSettable(entry.element, kAXSelectedTextRangeAttribute as CFString,
                                            &settable) == .success, settable.boolValue else {
            throw OpError.bad("element selected-text range is not settable")
        }
        guard let value = values[4] as? String else { throw OpError.bad("element has no string text value") }
        var range = try axSelectionRange(req, value: value)
        guard let rangeValue = AXValueCreate(.cfRange, &range) else { throw OpError.bad("could not create selected-text range") }
        status = AXUIElementSetAttributeValue(entry.element, kAXSelectedTextRangeAttribute as CFString, rangeValue)
    case "focus":
        status = AXUIElementSetAttributeValue(entry.element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    case "set_value":
        guard let text = req["value"] as? String else { throw OpError.bad("set_value needs string value") }
        var settable = DarwinBoolean(false)
        guard AXUIElementIsAttributeSettable(entry.element, kAXValueAttribute as CFString, &settable) == .success,
              settable.boolValue
        else { throw OpError.bad("element value is not settable") }
        status = AXUIElementSetAttributeValue(entry.element, kAXValueAttribute as CFString, text as CFString)
    default:
        throw OpError.bad("unknown ax_act action: \(action)")
    }
    var result: [String: Any] = ["action": action, "completed": status == .success]
    if let role = role { result["role"] = role }
    if let label = axString(values[2]) ?? axString(values[3]) { result["label"] = String(label.prefix(80)) }
    if let (pos, dim) = axFrame(values[6], values[7]) {
        result["x"] = Double(pos.x); result["y"] = Double(pos.y)
        result["w"] = Double(dim.width); result["h"] = Double(dim.height)
    }
    switch status {
    case .success: break
    case .cannotComplete: result["unanswered"] = true
    default: throw OpError.bad("ax_act \(action) failed: AXError \(status.rawValue)")
    }
    return result
}

// MARK: - Root keeper

/// Environment variable naming the JSON seed file the keeper loads when the
/// helper comes up (see `RootSeed`). Unset leaves the keeper dormant: the
/// helper behaves exactly as before this feature existed.
private let rootSeedEnvironment = "AVA_PERMISSIONS_HELPER_ROOT_SEED"

/// Timing knobs of the keeper state machine, in seconds.
private enum RootKeeperTiming {
    static let baseBackoffS = 0.5
    static let maxBackoffS = 30.0
    static let stableAfterS = 10.0
    static let conflictPollS = 5.0
}

func rootKeeperLog(_ message: String) {
    FileHandle.standardError.write(Data("AvaPermissionsHelper: root-keeper: \(message)\n".utf8))
}

/// Everything needed to launch one ava-root (the K3 `spawn_root` face),
/// validated fail-fast. Absolute paths only: the keeper runs without a shell
/// and under launchd's minimal environment, so nothing may rely on a shell or
/// on PATH resolution.
struct RootSeed {
    let argv: [String]
    let cwd: String
    let runDir: String
    let stdoutPath: String
    let stderrPath: String
    let environment: [String: String]

    static func from(_ raw: [String: Any]) throws -> RootSeed {
        guard let argv = raw["argv"] as? [String], !argv.isEmpty else {
            throw OpError.bad("root seed: argv must be a non-empty string list")
        }
        guard (argv[0] as NSString).isAbsolutePath else {
            throw OpError.bad("root seed: argv[0] must be an absolute path")
        }
        for (index, part) in argv.enumerated() where part.isEmpty {
            throw OpError.bad("root seed: argv[\(index)] must be non-empty")
        }
        guard let cwd = raw["cwd"] as? String, (cwd as NSString).isAbsolutePath else {
            throw OpError.bad("root seed: cwd must be an absolute path")
        }
        var isDirectory = ObjCBool(false)
        guard FileManager.default.fileExists(atPath: cwd, isDirectory: &isDirectory),
              isDirectory.boolValue
        else {
            throw OpError.bad("root seed: cwd is not an existing directory: \(cwd)")
        }
        guard let runDir = raw["run_dir"] as? String, (runDir as NSString).isAbsolutePath else {
            throw OpError.bad("root seed: run_dir must be an absolute path")
        }
        guard let stdoutPath = raw["stdout"] as? String,
              (stdoutPath as NSString).isAbsolutePath
        else {
            throw OpError.bad("root seed: stdout must be an absolute path")
        }
        guard let stderrPath = raw["stderr"] as? String,
              (stderrPath as NSString).isAbsolutePath
        else {
            throw OpError.bad("root seed: stderr must be an absolute path")
        }
        let environment: [String: String]
        if let rawEnvironment = raw["env"] {
            guard let parsedEnvironment = rawEnvironment as? [String: String] else {
                throw OpError.bad("root seed: env must be a map of string to string")
            }
            environment = parsedEnvironment
        } else {
            environment = [:]
        }
        return RootSeed(
            argv: argv,
            cwd: cwd,
            runDir: runDir,
            stdoutPath: stdoutPath,
            stderrPath: stderrPath,
            environment: environment
        )
    }

    /// Read a seed file (the startup path, `AVA_PERMISSIONS_HELPER_ROOT_SEED`).
    static func load(path: String) throws -> RootSeed {
        guard let data = FileManager.default.contents(atPath: path) else {
            throw OpError.bad("root seed: cannot read \(path)")
        }
        guard let mapping = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            throw OpError.bad("root seed: \(path) is not a JSON object")
        }
        return try from(mapping)
    }
}

/// Non-blocking liveness probe of `<run_dir>/ava-root.lock`, the run dir's
/// instance lock (flock; see `services/supervision/ava_root/singleton.py`). The lock is
/// the authority: the kernel releases it with its owner, so acquiring it
/// proves no live root holds the dir — no pid-reuse window. The pid recorded
/// in the file is read for reporting only. Both held and unknown ownership
/// block a spawn; root's own flock is the final single-instance arbiter.
enum RootDirLock {
    case free
    case held(pid: pid_t?)
    case unknown
}

func probeRootDirLock(runDir: String) -> RootDirLock {
    let lockPath = (runDir as NSString).appendingPathComponent("ava-root.lock")
    guard FileManager.default.fileExists(atPath: lockPath) else { return .free }
    let fd = open(lockPath, O_RDWR | O_CLOEXEC)
    guard fd >= 0 else { return .unknown }
    defer { close(fd) }
    if flock(fd, LOCK_EX | LOCK_NB) == 0 {
        _ = flock(fd, LOCK_UN)
        return .free
    }
    guard errno == EWOULDBLOCK else { return .unknown }
    var recordedPID: pid_t?
    if let text = try? String(contentsOfFile: lockPath, encoding: .utf8) {
        recordedPID = pid_t(text.split(separator: "\n").first ?? "")
    }
    return .held(pid: recordedPID)
}

/// Durable stop intent belongs to the outer owner. A restarted helper must
/// not revive a root that an acknowledged stop deliberately held down.
enum StopOwner: String {
    case root = "root-stopped"
    case helper = "helper-stopped"
}

struct StopIntent {
    static func path(_ runDir: String, owner: StopOwner) -> String {
        (runDir as NSString).appendingPathComponent(owner.rawValue)
    }

    static func flushDirectory(_ runDir: String) throws {
        let fd = open(runDir, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard fd >= 0 else { throw OpError.bad("cannot open root intent directory") }
        defer { close(fd) }
        guard fsync(fd) == 0 else { throw OpError.bad("cannot flush root intent directory") }
    }

    static func exists(_ runDir: String, owner: StopOwner) throws -> Bool {
        var info = stat()
        let result = lstat(path(runDir, owner: owner), &info)
        if result != 0, errno == ENOENT { return false }
        guard result == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_uid == getuid(), (info.st_mode & 0o077) == 0,
              try String(contentsOfFile: path(runDir, owner: owner), encoding: .utf8) == "stopped\n"
        else { throw OpError.bad("root stop intent is unreadable or invalid") }
        return true
    }

    static func store(_ runDir: String, owner: StopOwner) throws {
        if try exists(runDir, owner: owner) {
            try flushDirectory(runDir)
            return
        }
        let target = path(runDir, owner: owner)
        let fd = open(target, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw OpError.bad("cannot retain root stop intent") }
        defer { close(fd) }
        let bytes = Array("stopped\n".utf8)
        let count = bytes.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        guard count == bytes.count, fsync(fd) == 0 else {
            throw OpError.bad("root stop intent was not durably written")
        }
        try flushDirectory(runDir)
    }

    static func clear(_ runDir: String, owner: StopOwner) throws {
        if try exists(runDir, owner: owner) {
            guard unlink(path(runDir, owner: owner)) == 0 else { throw OpError.bad("cannot release root stop intent") }
        }
        try flushDirectory(runDir)
    }
}

func helperRunDir() throws -> String {
    guard let path = ProcessInfo.processInfo.environment[rootSeedEnvironment],
          (path as NSString).isAbsolutePath,
          (path as NSString).lastPathComponent == "seed.json" else {
        throw OpError.bad("helper lacks its home-bound root seed path")
    }
    return (path as NSString).deletingLastPathComponent
}

func helperShouldExitBeforeStart() throws -> Bool {
    try StopIntent.exists(helperRunDir(), owner: .helper)
}

func requestHelperShutdown(runDir: String) throws -> [String: Any] {
    try withChildOwnership {
        guard runDir == (try helperRunDir()) else { throw OpError.bad("helper shutdown home differs") }
        guard children.isEmpty else { throw OpError.bad("helper still owns execution children") }
        try rootKeeper.requireStoppedForHelperExitLocked()
        guard case .free = probeRootDirLock(runDir: runDir) else {
            throw OpError.bad("root native custody is live or unknown")
        }
        try StopIntent.store(runDir, owner: .helper)
        helperStopping = true
        return ["stopping": true, "pid": Int(getpid()), "run_dir": runDir]
    }
}

/// Owns the life of one ava-root process: seed it, keep it alive, and never
/// let it fight another instance for the same run dir.
///
/// The root is spawned as a direct child of the helper with the same detached
/// contract the helper uses for its sessions (posix_spawn, SETSID | CLOEXEC).
/// Per the K3 adapter contract, SETSID here is a session boundary only: the
/// root stays the launcher's direct child, ppid unchanged, no reparent —
/// I2's setsid prohibition is scoped to the units below the root.
///
/// Single instance: `ava-root.lock` is the authority. Before every spawn the
/// keeper probes it non-blockingly; a held lock means another live root owns
/// the run dir, so the keeper rests in `conflict` — the serving tree is left
/// alone, nothing is killed, and there is no crash loop ("lose attribution,
/// not service"). Foreign roots are never signalled; only a proven free run
/// directory permits another spawn.
///
/// An unexpected exit restarts root with bounded exponential backoff; an exit
/// this keeper requested does not restart. Dormant when no seed is configured.
final class RootKeeper {
    private let lock = childOwnershipLock
    private let queue = DispatchQueue(label: "ava.permissions-helper.root-keeper")

    // Everything below is guarded by `lock`.
    private var seed: RootSeed?
    private var seedError: String?
    private var state = "unseeded"
    private var childPID: pid_t?
    private var childStartedAt: Date?
    private var stopRequested = false
    private var failures = 0
    private var restarts = 0
    private var nextRestartAt: Date?
    private var lastExit: [String: Any]?
    private var conflictPID: pid_t?
    private var conflictSince: Date?

    // MARK: Wire surface

    /// Load the seed named by the environment (when present) and bring root
    /// up. Called once when the server starts; absent environment = dormant.
    func startIfConfigured() {
        guard let path = ProcessInfo.processInfo.environment[rootSeedEnvironment],
              !path.isEmpty
        else {
            return
        }
        do {
            try configure(try RootSeed.load(path: path), resume: false)
        } catch let OpError.bad(message) {
            lock.lock()
            seedError = message
            lock.unlock()
            rootKeeperLog("seed rejected: \(message)")
        } catch {
            lock.lock()
            seedError = "\(error)"
            lock.unlock()
            rootKeeperLog("seed rejected: \(error)")
        }
    }

    /// Store `newSeed` (replacing any previous one) and bring root up when
    /// the run dir allows. A seed never disrupts a live or stopping root —
    /// it applies to the next spawn. Re-seeding clears a previous stop intent.
    func configure(_ newSeed: RootSeed, resume: Bool = true) throws {
        lock.lock()
        if helperStopping || newSeed.runDir != (try? helperRunDir()) {
            lock.unlock()
            throw OpError.bad("helper admission is closed or root seed home differs")
        }
        let inFlight = childPID != nil || state == "stopping"
        if inFlight, seed?.runDir != newSeed.runDir {
            lock.unlock()
            throw OpError.bad("cannot replace the home of an owned root")
        }
        do {
            if resume && !inFlight { try StopIntent.clear(newSeed.runDir, owner: .root) }
            stopRequested = try StopIntent.exists(newSeed.runDir, owner: .root)
        } catch {
            lock.unlock()
            throw error
        }
        seed = newSeed
        seedError = nil
        if stopRequested { state = "stopped" }
        let shouldSpawn = !inFlight && !stopRequested
        lock.unlock()
        guard shouldSpawn else { return }
        queue.async { [weak self] in self?.attemptSpawn() }
    }

    /// Keeper snapshot for `root_status` and every mutating reply.
    func status() -> [String: Any] {
        lock.lock()
        defer { lock.unlock() }
        return statusLocked()
    }

    func requireStoppedForHelperExitLocked() throws {
        guard childPID == nil,
              (state == "stopped" && stopRequested) || (state == "unseeded" && seed == nil) else {
            throw OpError.bad("helper root custody is active or unknown")
        }
    }

    /// Retain stop intent before signalling the owned child. Unknown custody
    /// is never converted into authority to signal a foreign PID.
    func requestStop() throws -> [String: Any] {
        lock.lock()
        defer { lock.unlock() }
        let child = childPID
        let inConflict = state == "conflict"
        if child == nil, inConflict {
            throw OpError.bad("root custody is foreign or lost; explicit native recovery is required")
        }
        guard let seed else {
            throw OpError.bad("no retained root seed; cannot acknowledge durable stop")
        }
        try StopIntent.store(seed.runDir, owner: .root)
        stopRequested = true
        nextRestartAt = nil
        state = child == nil ? "stopped" : "stopping"
        if let child {
            _ = kill(child, SIGTERM)
            rootKeeperLog("stop requested; SIGTERM to root pid \(child)")
        }
        return statusLocked()
    }

    /// Called only inside withChildOwnership. Reaping and forgetting this PID
    /// are indivisible with root_stop's ownership check and signal delivery.
    func reapExitedChildLocked() {
        guard let pid = childPID else { return }
        var exitStatus: Int32 = 0
        guard waitpid(pid, &exitStatus, WNOHANG) == pid else { return }
        childPID = nil
        let startedAt = childStartedAt
        childStartedAt = nil
        let requested = stopRequested || state == "stopping"
        if requested {
            lastExit = ["kind": "stopped", "at": Date().timeIntervalSince1970]
            state = "stopped"
            rootKeeperLog("root stopped by request")
            return
        }
        let exit = Self.describeExit(exitStatus)
        lastExit = exit
        let uptime = startedAt.map { Date().timeIntervalSince($0) } ?? 0
        if uptime >= RootKeeperTiming.stableAfterS {
            failures = 0
        }
        let delay = min(
            RootKeeperTiming.maxBackoffS,
            RootKeeperTiming.baseBackoffS * pow(2.0, Double(failures))
        )
        failures += 1
        restarts += 1
        nextRestartAt = Date().addingTimeInterval(delay)
        state = "backoff"
        rootKeeperLog("root exited unexpectedly (\(exit["kind"] ?? "?")); restart in \(delay)s")
        queue.asyncAfter(deadline: .now() + delay) { [weak self] in self?.attemptSpawn() }
    }

    // MARK: Internals

    /// One spawn attempt: probe the lock, then either rest or launch root.
    private func attemptSpawn() {
        lock.lock()
        guard let seed = self.seed, !helperStopping, !stopRequested,
              childPID == nil, state != "conflict" else {
            lock.unlock()
            return
        }
        lock.unlock()

        switch probeRootDirLock(runDir: seed.runDir) {
        case .held(let pid):
            lock.lock()
            state = "conflict"
            conflictPID = pid
            conflictSince = Date()
            nextRestartAt = nil
            lock.unlock()
            let detail = pid.map { "pid \($0)" } ?? "pid unreadable"
            rootKeeperLog("another root owns \(seed.runDir) (\(detail)); waiting, not spawning")
            scheduleConflictPoll()
            return
        case .free:
            break
        case .unknown:
            lock.lock()
            seedError = "root ownership lock is unreadable; custody is unknown"
            state = "conflict"
            lock.unlock()
            return
        }

        var environment = ProcessInfo.processInfo.environment
        for (key, value) in seed.environment {
            environment[key] = value
        }
        // Spawn and ownership accounting share one lock domain with
        // native waitpid and `reapExitedChildLocked`: a root that exits before
        // its pid is recorded would
        // otherwise be reaped by the SIGCHLD drain against a still-nil
        // `childPID`, dropping that exit and parking the keeper on a dead pid
        // it never restarts. Under one lock, a fast exit drains only after
        // `childPID` is set and is attributed like any other exit (the same
        // discipline the session table's spawn uses; QA #3242).
        lock.lock()
        guard !helperStopping, !stopRequested, childPID == nil, state != "stopping",
              self.seed?.runDir == seed.runDir else {
            lock.unlock()
            return
        }
        do {
            if try StopIntent.exists(seed.runDir, owner: .root) {
                stopRequested = true
                state = "stopped"
                lock.unlock()
                return
            }
            let pid = try spawnDetachedChild(
                argv: seed.argv,
                environment: environment,
                cwd: seed.cwd,
                stdoutPath: seed.stdoutPath,
                stderrPath: seed.stderrPath
            )
            childPID = pid
            childStartedAt = Date()
            state = "running"
            nextRestartAt = nil
            conflictPID = nil
            conflictSince = nil
            lock.unlock()
            rootKeeperLog("ava-root started (pid \(pid))")
        } catch {
            // scheduleSpawnRetry takes the same lock; release before reporting.
            lock.unlock()
            if let opFailure = error as? OpError, case .bad(let message) = opFailure {
                scheduleSpawnRetry(reason: message)
            } else {
                scheduleSpawnRetry(reason: "\(error)")
            }
        }
    }

    private func scheduleSpawnRetry(reason: String) {
        lock.lock()
        lastExit = ["kind": "spawn-failed", "detail": reason, "at": Date().timeIntervalSince1970]
        let delay = min(
            RootKeeperTiming.maxBackoffS,
            RootKeeperTiming.baseBackoffS * pow(2.0, Double(failures))
        )
        failures += 1
        nextRestartAt = Date().addingTimeInterval(delay)
        state = "backoff"
        lock.unlock()
        rootKeeperLog("root spawn failed (\(reason)); retry in \(delay)s")
        queue.asyncAfter(deadline: .now() + delay) { [weak self] in self?.attemptSpawn() }
    }

    /// While a foreign root holds the run dir, watch it; the moment the dir
    /// frees, seed again (unless a stop intent is standing).
    private func scheduleConflictPoll() {
        queue.asyncAfter(deadline: .now() + RootKeeperTiming.conflictPollS) { [weak self] in
            self?.pollConflict()
        }
    }

    private func pollConflict() {
        lock.lock()
        guard state == "conflict", let seed = self.seed else {
            lock.unlock()
            return
        }
        let stopWanted = stopRequested
        lock.unlock()
        if case .free = probeRootDirLock(runDir: seed.runDir) {
            lock.lock()
            conflictPID = nil
            conflictSince = nil
            state = "stopped"
            lock.unlock()
            rootKeeperLog("run dir is free again")
            if !stopWanted {
                attemptSpawn()
            }
        } else {
            scheduleConflictPoll()
        }
    }

    private static func describeExit(_ status: Int32) -> [String: Any] {
        // Decode the BSD wait status by hand: WIFEXITED / WEXITSTATUS and
        // friends are function-like C macros Swift cannot import.
        let signalBits = status & 0x7f
        if signalBits == 0 {
            let code = Int((status >> 8) & 0xff)
            let kind = code == 0 ? "clean" : (code == 1 ? "refused" : "crash")
            return ["kind": kind, "code": code, "at": Date().timeIntervalSince1970]
        }
        return [
            "kind": "crash",
            "signal": Int(signalBits),
            "at": Date().timeIntervalSince1970,
        ]
    }

    private func statusLocked() -> [String: Any] {
        var out: [String: Any] = [
            "state": state,
            "seeded": seed != nil,
            "restarts": restarts,
            "stop_requested": stopRequested,
        ]
        if let seed {
            out["run_dir"] = seed.runDir
            // The launch face this keeper would spawn next (crash restart). The
            // environment is withheld: it carries the root's private secrets.
            out["seed"] = [
                "argv": seed.argv,
                "cwd": seed.cwd,
                "run_dir": seed.runDir,
                "stdout": seed.stdoutPath,
                "stderr": seed.stderrPath,
            ]
        }
        if let seedError {
            out["seed_error"] = seedError
        }
        if let childPID {
            out["pid"] = Int(childPID)
        }
        if let lastExit {
            out["last_exit"] = lastExit
        }
        if let nextRestartAt {
            out["next_restart_in_s"] = max(0, nextRestartAt.timeIntervalSinceNow)
        }
        if state == "conflict" {
            var conflict: [String: Any] = [:]
            if let conflictPID {
                conflict["pid"] = Int(conflictPID)
            }
            if let conflictSince {
                conflict["since"] = conflictSince.timeIntervalSince1970
            }
            out["conflict"] = conflict
        }
        return out
    }
}

private let rootKeeper = RootKeeper()

// MARK: - Dispatch


func dispatch(_ req: [String: Any]) -> [String: Any] {
    let id = req["id"]
    let method = req["method"] as? String ?? ""
    let axGatedMethods: Set<String> = ["click", "drag", "move", "focus_app", "type", "key", "scroll", "ax_window_info", "ax_tree", "ax_act"]
    if axGatedMethods.contains(method) && !axTrustedOrPrompt() {
        return ["id": id as Any, "ok": false, "error": axGrantError]
    }
    do {
        let result: Any
        switch method {
        case "ping":
            result = ["pong": true, "pid": Int(getpid()), "root_stop_intent_v1": true, "helper_shutdown_v1": true,
                      "root_seed_report_v1": true, "ax_tree_v1": true, "ax_act_v1": true, "ax_act_v2": true, "native_input_v1": true,
                      "preflight_screen": CGPreflightScreenCaptureAccess(),
                      "ax_trusted": AXIsProcessTrusted()]
        case "file_list": result = try fileList(req)
        case "file_read": result = try fileRead(req)
        case "list_apps": result = listApps()
        case "list_windows": result = try listWindows(req)
        case "focus_app": result = try focusApp(req)
        case "screencapture_window": result = try screencaptureWindow(req)
        case "screencapture_region": result = try screencaptureRegion(req)
        case "click": result = try click(req)
        case "drag": result = try drag(req)
        case "type": result = try typeText(req)
        case "key": result = try key(req)
        case "scroll": result = try scroll(req)
        case "move": result = try mouseMove(req)
        case "cursor_position": result = try cursorPosition()
        case "ax_window_info": result = try axWindowInfo(req)
        case "ax_tree": result = try axTree(req)
        case "ax_act": result = try axAct(req)
        case "window_info": result = try windowInfo(req)
        case "session_info": result = sessionInfo()
        case "screen_size": result = try screenSize(req)
        case "frontmost_app": result = frontmostApp()
        case "spawn": result = try spawnProcess(req)
        case "session_list": result = try sessionList(req)
        case "session_has": result = try sessionHas(req)
        case "signal": result = try signalSession(req)
        case "root_seed":
            guard let config = req["config"] as? [String: Any] else {
                throw OpError.bad("root_seed needs a config object")
            }
            try rootKeeper.configure(try RootSeed.from(config))
            result = rootKeeper.status()
        case "root_status": result = rootKeeper.status()
        case "root_stop":
            if req["force"] != nil { throw OpError.bad("root_stop does not accept foreign-PID force") }
            result = try rootKeeper.requestStop()
        case "helper_shutdown":
            guard let runDir = req["run_dir"] as? String else {
                throw OpError.bad("helper_shutdown needs its bound run_dir")
            }
            result = try requestHelperShutdown(runDir: runDir)
        default:
            return ["id": id as Any, "ok": false, "error": "unknown method: \(method)"]
        }
        return ["id": id as Any, "ok": true, "result": result]
    } catch let OpError.bad(msg) {
        return ["id": id as Any, "ok": false, "error": msg]
    } catch {
        return ["id": id as Any, "ok": false, "error": "\(error)"]
    }
}

// MARK: - Panel mode

/// Panel-mode copy: Localizable.strings from the app bundle (source lives in
/// helper/locales/<lang>.lproj, copied into Contents/Resources at build; en is
/// the base catalog, zh-Hans ships alongside). A missing catalog falls back to
/// the key itself, so a bare-binary dev run shows raw keys -- expected.
func panelString(_ key: String) -> String {
    NSLocalizedString(key, comment: "")
}

/// Launch parameters for a panel instance, parsed from argv. The launchd
/// daemon never reaches this code (it always carries a socket path).
struct PanelArgs {
    var repo: String?
    var helperSocket: String?
    var tier = "L2"

    static func parse(_ argv: [String]) -> PanelArgs {
        var args = PanelArgs()
        var index = 1
        while index < argv.count {
            let flag = argv[index]
            let value = index + 1 < argv.count ? argv[index + 1] : nil
            switch flag {
            case "--repo":
                args.repo = value
                index += 2
            case "--helper-socket":
                args.helperSocket = value
                index += 2
            case "--tier":
                if let value = value {
                    args.tier = value
                }
                index += 2
            default:
                index += 1
            }
        }
        return args
    }
}

/// The panel controller: grant matrix, tier picker, the one-click fill-missing
/// run driven through scripts/host_ops/tcc/onboard-helper-grants.py (the single source
/// of truth for probes and triggers), and a live run log.
final class PanelDelegate: NSObject, NSApplicationDelegate, NSTableViewDataSource, NSTableViewDelegate {
    private static let rowOrder = [
        "desktop", "documents", "downloads",
        "apple-events:Finder", "apple-events:Terminal", "apple-events:System Events",
        "apple-events:Safari", "apple-events:Google Chrome",
        "screen-recording", "accessibility",
        "appdata", "media", "icloud", "fda", "devtools",
    ]

    private let args: PanelArgs
    private var window: NSWindow?
    private let tierPopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let refreshButton = NSButton(frame: .zero)
    private let fixButton = NSButton(frame: .zero)
    private let spinner = NSProgressIndicator(frame: .zero)
    private let statusLabel = NSTextField(labelWithString: "")
    private let tableView = NSTableView(frame: .zero)
    private let logView = NSTextView(frame: NSRect(x: 0, y: 0, width: 100, height: 80))
    private var rows: [(name: String, status: String)] = []

    private var process: Process?
    private var logHandle: FileHandle?
    private var logPath: String?
    private var logOffset = 0
    private var pollTimer: Timer?
    private var reportsBeforeRun: Set<String> = []
    private let workdir = "/tmp/ava-panel-tcc"

    init(args: PanelArgs) {
        self.args = args
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        if let repo = args.repo,
           FileManager.default.fileExists(atPath: repo + "/scripts/host_ops/tcc/onboard-helper-grants.py") {
            runTool(check: true)
        } else {
            setStatus(panelString("panel.error.noRepo"), color: .systemRed)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        process?.terminate()
    }

    private func buildWindow() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 780, height: 560),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = panelString("panel.title")
        window.isReleasedWhenClosed = false
        self.window = window

        tierPopup.addItems(withTitles: ["L1", "L2", "L3"])
        tierPopup.selectItem(withTitle: args.tier)
        refreshButton.title = panelString("panel.refresh")
        refreshButton.target = self
        refreshButton.action = #selector(refreshTapped)
        refreshButton.bezelStyle = .rounded
        fixButton.title = panelString("panel.fix")
        fixButton.target = self
        fixButton.action = #selector(fixTapped)
        fixButton.bezelStyle = .rounded
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let topBar = NSStackView(views: [
            NSTextField(labelWithString: panelString("panel.tier")),
            tierPopup,
            refreshButton,
            fixButton,
            spinner,
            statusLabel,
        ])
        topBar.orientation = .horizontal
        topBar.spacing = 8
        topBar.alignment = .centerY

        let serviceColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("service"))
        serviceColumn.title = panelString("panel.col.item")
        serviceColumn.width = 420
        let statusColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("status"))
        statusColumn.title = panelString("panel.col.status")
        statusColumn.width = 300
        tableView.addTableColumn(serviceColumn)
        tableView.addTableColumn(statusColumn)
        tableView.dataSource = self
        tableView.delegate = self
        tableView.usesAlternatingRowBackgroundColors = true
        let tableScroll = NSScrollView(frame: .zero)
        tableScroll.documentView = tableView
        tableScroll.hasVerticalScroller = true
        tableScroll.borderType = .bezelBorder

        logView.isEditable = false
        logView.isSelectable = true
        logView.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        logView.minSize = NSSize(width: 0, height: 0)
        logView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        logView.isVerticallyResizable = true
        logView.isHorizontallyResizable = false
        logView.autoresizingMask = [.width]
        logView.textContainer?.widthTracksTextView = true
        let logScroll = NSScrollView(frame: .zero)
        logScroll.documentView = logView
        logScroll.hasVerticalScroller = true
        logScroll.borderType = .bezelBorder

        let root = NSStackView(views: [topBar, tableScroll, logScroll])
        root.orientation = .vertical
        root.spacing = 10
        root.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        root.translatesAutoresizingMaskIntoConstraints = false
        tableScroll.translatesAutoresizingMaskIntoConstraints = false
        tableScroll.heightAnchor.constraint(equalToConstant: 240).isActive = true

        if let content = window.contentView {
            content.addSubview(root)
            NSLayoutConstraint.activate([
                root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
                root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
                root.topAnchor.constraint(equalTo: content.topAnchor),
                root.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            ])
        }

        window.center()
        window.makeKeyAndOrderFront(nil)
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    @objc private func refreshTapped() {
        runTool(check: true)
    }

    @objc private func fixTapped() {
        runTool(check: false)
    }

    private func runTool(check: Bool) {
        guard process == nil else {
            return
        }
        guard let repo = args.repo else {
            setStatus(panelString("panel.error.noRepo"), color: .systemRed)
            return
        }
        let python = repo + "/.venv/bin/python"
        let tool = repo + "/scripts/host_ops/tcc/onboard-helper-grants.py"
        let fileManager = FileManager.default
        guard fileManager.fileExists(atPath: python), fileManager.fileExists(atPath: tool) else {
            setStatus(panelString("panel.error.noRepo"), color: .systemRed)
            return
        }
        try? fileManager.createDirectory(atPath: workdir, withIntermediateDirectories: true)
        let existing = (try? fileManager.contentsOfDirectory(atPath: workdir)) ?? []
        reportsBeforeRun = Set(existing.filter { $0.hasPrefix("report-") })
        let logName = "panel-run-" + String(Int(Date().timeIntervalSince1970)) + ".log"
        let logFullPath = workdir + "/" + logName
        logPath = logFullPath
        logOffset = 0
        _ = fileManager.createFile(atPath: logFullPath, contents: nil)
        logHandle = FileHandle(forWritingAtPath: logFullPath)

        let task = Process()
        task.executableURL = URL(fileURLWithPath: python)
        var arguments = [tool, "--tier", tierPopup.titleOfSelectedItem ?? args.tier, "--workdir", workdir]
        if check {
            arguments.append("--check")
        } else {
            // The fill button click is the user-present attestation: pass the
            // guard pair the CLI requires (--fill-pending is refused without
            // --confirm-user-present). Extended-group dialogs follow.
            // Note: an icloud (FileProviderDomain) prompt writes no
            // PROMPTING log line -- verify it by screenshot, not logs.
            arguments.append("--fill-pending")
            arguments.append("--confirm-user-present")
        }
        task.arguments = arguments
        task.currentDirectoryURL = URL(fileURLWithPath: repo)
        var environment = ProcessInfo.processInfo.environment
        if let socket = args.helperSocket {
            environment["AVA_PERMISSIONS_HELPER_SOCKET"] = socket
        }
        task.environment = environment
        task.standardOutput = logHandle
        task.standardError = logHandle
        task.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async { self?.runFinished() }
        }
        do {
            try task.run()
        } catch {
            logHandle = nil
            setStatus(panelString("panel.error.launch"), color: .systemRed)
            return
        }
        process = task
        setRunning(true)
        setStatus(panelString(check ? "panel.status.checking" : "panel.status.running"), color: .secondaryLabelColor)
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.pollLog()
        }
    }

    private func runFinished() {
        pollTimer?.invalidate()
        pollTimer = nil
        process = nil
        logHandle = nil
        setRunning(false)
        pollLog()
        loadLatestReport()
    }

    private func loadLatestReport() {
        let fileManager = FileManager.default
        let names = (try? fileManager.contentsOfDirectory(atPath: workdir)) ?? []
        let fresh = names.filter { $0.hasPrefix("report-") && $0.hasSuffix(".json") && !reportsBeforeRun.contains($0) }
        guard let latest = fresh.sorted().last,
              let data = fileManager.contents(atPath: workdir + "/" + latest),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            setStatus(panelString("panel.error.report"), color: .systemRed)
            return
        }
        let statuses = (object["statuses"] as? [String: String]) ?? [:]
        rows = orderedRows(from: statuses)
        tableView.reloadData()
        let unresolved = (object["unresolved"] as? Bool) ?? true
        if unresolved {
            // One count rule lives in the tool; the panel renders the reported number.
            let count = (object["unresolved_count"] as? Int) ?? 0
            setStatus(String(format: panelString("panel.status.unresolved"), count), color: .systemOrange)
        } else {
            setStatus(panelString("panel.status.pass"), color: .systemGreen)
        }
    }

    private func pollLog() {
        guard let logPath = logPath, let handle = FileHandle(forReadingAtPath: logPath) else {
            return
        }
        defer { try? handle.close() }
        guard (try? handle.seek(toOffset: UInt64(logOffset))) != nil else {
            return
        }
        let data = (try? handle.readToEnd()) ?? Data()
        guard !data.isEmpty else {
            return
        }
        logOffset += data.count
        if let text = String(data: data, encoding: .utf8) {
            appendLog(text)
        }
    }

    private func orderedRows(from statuses: [String: String]) -> [(name: String, status: String)] {
        let entries = statuses.map { (name: $0.key, status: $0.value) }
        return entries.sorted { left, right in
            let leftRank = Self.rowOrder.firstIndex(of: left.name) ?? Int.max
            let rightRank = Self.rowOrder.firstIndex(of: right.name) ?? Int.max
            if leftRank != rightRank {
                return leftRank < rightRank
            }
            return left.name < right.name
        }
    }

    private func appendLog(_ text: String) {
        logView.textStorage?.append(NSAttributedString(string: text))
        logView.scrollToEndOfDocument(nil)
    }

    private func setStatus(_ text: String, color: NSColor) {
        statusLabel.stringValue = text
        statusLabel.textColor = color
    }

    private func setRunning(_ running: Bool) {
        refreshButton.isEnabled = !running
        fixButton.isEnabled = !running
        tierPopup.isEnabled = !running
        if running {
            spinner.startAnimation(nil)
        } else {
            spinner.stopAnimation(nil)
        }
    }

    func numberOfRows(in tableView: NSTableView) -> Int {
        return rows.count
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard let tableColumn = tableColumn else {
            return nil
        }
        let entry = rows[row]
        let text = tableColumn.identifier.rawValue == "service" ? entry.name : entry.status
        let label = NSTextField(labelWithString: text)
        label.lineBreakMode = .byTruncatingTail
        if tableColumn.identifier.rawValue == "status" {
            label.textColor = statusColor(for: entry.status)
        }
        return label
    }

    private func statusColor(for status: String) -> NSColor {
        if status.contains("granted") {
            return .systemGreen
        }
        if status.contains("denied") || status.contains("missing") {
            return .systemRed
        }
        if status.contains("unresolved") || status.contains("pending") {
            return .systemOrange
        }
        return .labelColor
    }
}

/// Run as the user-facing panel; never returns. Reached only without a socket.
func runPanelMode() -> Never {
    let args = PanelArgs.parse(CommandLine.arguments)
    let app = NSApplication.shared
    _ = app.setActivationPolicy(.accessory)
    let delegate = PanelDelegate(args: args)
    app.delegate = delegate
    app.run()
    exit(0)
}

// MARK: - Socket server

func socketPath() -> String {
    if let p = ProcessInfo.processInfo.environment["AVA_PERMISSIONS_HELPER_SOCKET"] { return p }
    if CommandLine.arguments.count > 1, !CommandLine.arguments[1].hasPrefix("--") {
        return CommandLine.arguments[1]
    }
    // No socket path: this process is not the launchd daemon -- it is a
    // user-facing launch (Finder double-click / `open -n -a`), so it becomes
    // the panel instance instead of exiting.
    FileHandle.standardError.write(Data("AvaPermissionsHelper: no socket path -- starting panel mode\n".utf8))
    runPanelMode()
}

/// Register the helper into the Screen Recording and Accessibility lists (and
/// prompt once if the session allows), so the operator grants by flipping a
/// toggle rather than hunting via the "+" button. No effect once granted.
func registerPermissions() {
    if ProcessInfo.processInfo.environment[skipRegistrationEnvironment] == "1" {
        FileHandle.standardError.write(
            Data("AvaPermissionsHelper: permission registration skipped (test affordance)\n".utf8)
        )
        return
    }
    if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
    let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    _ = AXIsProcessTrustedWithOptions(opts)
}

/// Serve desktop requests only through an owner-only Unix socket. `chmod` locks
/// the socket file to mode 0700, and `getpeereid` admits only this process's
/// uid; same-uid processes remain the documented residual threat surface.
func serve() {
    let path = socketPath()
    do {
        if try helperShouldExitBeforeStart() { exit(0) }
    } catch {
        FileHandle.standardError.write(Data("helper stop intent is unknown: \(error)\n".utf8))
        exit(1)
    }
    reportResponsiblePID()
    registerPermissions()
    unlink(path)
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    if fd < 0 { perror("socket"); exit(1) }

    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let pathBytes = Array(path.utf8)
    guard pathBytes.count < MemoryLayout.size(ofValue: addr.sun_path) else {
        FileHandle.standardError.write(Data("socket path too long: \(path)\n".utf8)); exit(1)
    }
    withUnsafeMutablePointer(to: &addr.sun_path) { p in
        p.withMemoryRebound(to: UInt8.self, capacity: pathBytes.count) { dst in
            for (i, b) in pathBytes.enumerated() { dst[i] = b }
        }
    }
    let len = socklen_t(MemoryLayout<sockaddr_un>.size)
    let bindRC = withUnsafePointer(to: &addr) { ptr in
        ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, len) }
    }
    if bindRC < 0 { perror("bind"); exit(1) }
    // This helper holds TCC-granted desktop access, so the socket must be
    // owner-only: a foreign local process must not drive screenshots or clicks.
    // Same-uid processes are the documented residual threat surface.
    if chmod(path, mode_t(0o700)) != 0 { perror("chmod"); exit(1) }
    if listen(fd, 16) < 0 { perror("listen"); exit(1) }
    guard setCloseOnExec(fd, true) else {
        perror("fcntl"); exit(1)
    }
    startChildReaper()
    rootKeeper.startIfConfigured()
    FileHandle.standardError.write(Data("AvaPermissionsHelper: listening on \(path)\n".utf8))

    while true {
        let conn = accept(fd, nil, nil)
        if conn < 0 {
            if errno == EINTR || errno == ECONNABORTED { continue }
            perror("accept")
            usleep(100_000)  // a persistent error (e.g. fd exhaustion) must not become a tight CPU spin
            continue
        }
        guard setCloseOnExec(conn, true) else {
            close(conn)
            continue
        }
        var peerUID: uid_t = 0
        var peerGID: gid_t = 0
        if getpeereid(conn, &peerUID, &peerGID) != 0 || peerUID != getuid() {
            close(conn)
            continue
        }
        while let line = readLine(conn) {
            let req = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any]
            if let req = req {
                writeJSONLine(conn, dispatch(req))
                if withChildOwnership({ helperStopping }) {
                    close(conn)
                    close(fd)
                    unlink(path)
                    exit(0)
                }
            } else {
                writeJSONLine(conn, ["id": NSNull(), "ok": false, "error": "JSON parse error"])
            }
        }
        close(conn)
    }
}

serve()
