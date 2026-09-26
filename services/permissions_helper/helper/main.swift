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
    // 2026-09-12; same fix family as shared/sessions/pty/launch.py's SIG_DFL
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
// SECURITY SYNC: tests/services/test_permissions_helper.py::
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

/// Post a synthetic mouse click at a global screen coordinate (move + down + up).
/// Pass "double": true for a second click. Dispatch refuses it without the
/// Accessibility grant instead of posting events macOS would silently drop.
func click(_ req: [String: Any]) throws -> [String: Any] {
    guard let x = numericDouble(req["x"]), let y = numericDouble(req["y"])
    else { throw OpError.bad("click needs numeric x,y") }
    let double = (req["double"] as? Bool) ?? false
    let pt = CGPoint(x: x, y: y)
    func once() throws {
        for kind in [CGEventType.mouseMoved, .leftMouseDown, .leftMouseUp] {
            guard let ev = CGEvent(mouseEventSource: nil, mouseType: kind,
                                   mouseCursorPosition: pt, mouseButton: .left)
            else { throw OpError.bad("could not create mouse event") }
            ev.post(tap: .cghidEventTap)
        }
    }
    try once()
    if double { try once() }
    return ["clicked": ["x": x, "y": y], "double": double]
}

/// Post a single key down/up by virtual keycode, optionally with Command held.
/// Flags are set explicitly (0 when no modifier) so a plain key after a Cmd+key
/// event cannot inherit a stale Command flag. Dispatch refuses it without the
/// Accessibility grant instead of posting events macOS would silently drop.
func key(_ req: [String: Any]) throws -> [String: Any] {
    guard let code = req["code"] as? Int else { throw OpError.bad("key needs int code") }
    let cmd = (req["cmd"] as? Bool) ?? false
    let flags: CGEventFlags = cmd ? .maskCommand : []
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: CGKeyCode(code), keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: CGKeyCode(code), keyDown: false)
    else { throw OpError.bad("could not create key event") }
    down.flags = flags
    up.flags = flags
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
    return ["key": code, "cmd": cmd]
}

/// Move the cursor to (x, y) then post a vertical scroll of `dy` pixels (negative
/// scrolls toward older content). Dispatch refuses it without the Accessibility
/// grant instead of posting events macOS would silently drop.
func scroll(_ req: [String: Any]) throws -> [String: Any] {
    guard let x = numericDouble(req["x"]), let y = numericDouble(req["y"]),
          let dy = req["dy"] as? Int
    else { throw OpError.bad("scroll needs numeric x,y and int dy") }
    if let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
                          mouseCursorPosition: CGPoint(x: x, y: y), mouseButton: .left) {
        move.post(tap: .cghidEventTap)
    }
    guard let ev = CGEvent(scrollWheelEvent2Source: nil, units: .pixel,
                           wheelCount: 1, wheel1: Int32(dy), wheel2: 0, wheel3: 0)
    else { throw OpError.bad("could not create scroll event") }
    ev.post(tap: .cghidEventTap)
    return ["scrolled": dy]
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
/// (physical) to click coordinates (logical) via `scale`; Windows has no
/// backing scale (physical == logical, scale is always 1).
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
/// instance lock (flock; see `services/ava_root/singleton.py`). The lock is
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
        if let seed { out["run_dir"] = seed.runDir }
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
    let axGatedMethods: Set<String> = ["click", "type", "key", "scroll", "ax_window_info"]
    if axGatedMethods.contains(method) && !axTrustedOrPrompt() {
        return ["id": id as Any, "ok": false, "error": axGrantError]
    }
    do {
        let result: Any
        switch method {
        case "ping":
            result = ["pong": true, "pid": Int(getpid()), "root_stop_intent_v1": true, "helper_shutdown_v1": true,
                      "finite_executor_v1": true, "preflight_screen": CGPreflightScreenCaptureAccess(),
                      "ax_trusted": AXIsProcessTrusted()]
        case "file_list": result = try fileList(req)
        case "file_read": result = try fileRead(req)
        case "screencapture_region": result = try screencaptureRegion(req)
        case "click": result = try click(req)
        case "type": result = try typeText(req)
        case "key": result = try key(req)
        case "scroll": result = try scroll(req)
        case "ax_window_info": result = try axWindowInfo(req)
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
/// run driven through scripts/tcc-onboard-helper-grants.py (the single source
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
           FileManager.default.fileExists(atPath: repo + "/scripts/tcc-onboard-helper-grants.py") {
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
        let tool = repo + "/scripts/tcc-onboard-helper-grants.py"
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

// MARK: - Finite release-executor mode

// One launchd job per release-operation attempt runs this same signed binary as
// `AvaPermissionsHelper --finite-executor v1 --cwd DIR [--env KEY=VALUE]... --
// EXECUTABLE [ARG]...`. The mode is entered before any desktop-helper setup:
// no permission registration, socket, session table, root keeper or restart.
//
// It spawns exactly one executor in launchd's job process group (no SETSID or
// SETPGROUP), so launchd's documented same-group cleanup covers the executor
// and every finite descendant that stays in the group. The executor environment
// is built only from the explicit `--env` pairs: launchd supplements a job's
// environment from its session domain, and none of that may reach the executor.
// The direct child is observed with WNOWAIT and reaped under the same lock that
// forwards signals, so a forwarded signal can never reach a reused PID. The mode
// makes no release decision; its exit code only names the executor's outcome.
//
// EXIT SYNC: cli/release_transition/launcher_macos.py::FINITE_EXIT mirrors
// these codes. Update both together.

let finiteExecutorFlag = "--finite-executor"
let finiteExecutorVersion = "v1"

enum FiniteExit: Int32 {
    case executorSucceeded = 0
    case usage = 64
    case notJobLeader = 65
    case spawnFailed = 71
    case interruptedBeforeSpawn = 75
    case executorFailed = 80
    case executorSignaled = 81
    case custodyLost = 82
}

struct FiniteSpec {
    let cwd: String
    let environment: [String: String]
    let argv: [String]
}

/// Direct-child state shared by the waiting thread and signal forwarding.
private let finiteLock = NSLock()
private var finiteChild: pid_t?
private var finiteReaped = false
private var finiteInterrupted = false
private var finiteSignalSources: [DispatchSourceSignal] = []
private let finiteForwardedSignals: [Int32] = [SIGTERM, SIGINT, SIGHUP]

func finiteLog(_ fields: [String: Any]) {
    var record = fields
    record["finite_executor"] = finiteExecutorVersion
    guard var data = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
    else { return }
    data.append(0x0A)
    FileHandle.standardError.write(data)
}

func finiteRefuse(_ code: FiniteExit, _ message: String) -> Never {
    finiteLog(["refused": message, "exit": Int(code.rawValue)])
    exit(code.rawValue)
}

private func validEnvironmentKey(_ key: Substring) -> Bool {
    guard let first = key.unicodeScalars.first,
          first == "_" || ("A"..."Z").contains(first)
    else { return false }
    return key.unicodeScalars.allSatisfy {
        $0 == "_" || ("A"..."Z").contains($0) || ("0"..."9").contains($0)
    }
}

/// Parse the complete argv grammar; anything else refuses before a spawn.
func parseFiniteSpec(_ arguments: [String]) throws -> FiniteSpec {
    guard arguments.first == finiteExecutorVersion else {
        throw OpError.bad("finite executor protocol version must be \(finiteExecutorVersion)")
    }
    var index = 1
    var cwd: String?
    var environment: [String: String] = [:]
    while index < arguments.count, arguments[index] != "--" {
        guard index + 1 < arguments.count else { throw OpError.bad("option lacks a value") }
        let value = arguments[index + 1]
        switch arguments[index] {
        case "--cwd":
            guard cwd == nil, (value as NSString).isAbsolutePath else {
                throw OpError.bad("--cwd must be one absolute directory")
            }
            cwd = value
        case "--env":
            let parts = value.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2, validEnvironmentKey(parts[0]),
                  environment[String(parts[0])] == nil
            else { throw OpError.bad("--env needs one unique KEY=VALUE") }
            environment[String(parts[0])] = String(parts[1])
        default:
            throw OpError.bad("unknown finite executor option")
        }
        index += 2
    }
    guard index < arguments.count, let directory = cwd else {
        throw OpError.bad("finite executor needs --cwd and a -- separated command")
    }
    let argv = Array(arguments[(index + 1)...])
    guard let executable = argv.first, (executable as NSString).isAbsolutePath,
          argv.allSatisfy({ !$0.isEmpty })
    else { throw OpError.bad("executor argv needs an absolute executable and no empty argument") }
    var isDirectory = ObjCBool(false)
    guard FileManager.default.fileExists(atPath: directory, isDirectory: &isDirectory),
          isDirectory.boolValue
    else { throw OpError.bad("executor cwd is not an existing directory") }
    return FiniteSpec(cwd: directory, environment: environment, argv: argv)
}

private func finiteForward(_ signalValue: Int32) {
    finiteLock.lock()
    defer { finiteLock.unlock() }
    if let child = finiteChild {
        // The child is unreaped until the waiter marks it reaped under this
        // lock, so this PID still names the executor.
        if !finiteReaped { _ = kill(child, signalValue) }
    } else {
        finiteInterrupted = true
    }
}

/// kqueue records every delivery even while the disposition is SIG_IGN, so
/// installing the sources before spawning loses no termination request.
private func installFiniteSignalForwarding() {
    let queue = DispatchQueue(label: "ava.permissions-helper.finite-signals")
    for signalValue in finiteForwardedSignals {
        _ = signal(signalValue, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: signalValue, queue: queue)
        source.setEventHandler { finiteForward(signalValue) }
        source.resume()
        finiteSignalSources.append(source)
    }
}

private func withCStrings<T>(
    _ values: [String], _ body: (inout [UnsafeMutablePointer<CChar>?]) throws -> T
) throws -> T {
    var pointers = values.map { strdup($0) }
    defer { pointers.forEach { if let pointer = $0 { free(pointer) } } }
    guard pointers.allSatisfy({ $0 != nil }) else { throw OpError.bad("cannot allocate spawn strings") }
    pointers.append(nil)
    return try body(&pointers)
}

/// Spawn in this job's process group: no SETSID, no SETPGROUP.
private func spawnFiniteExecutor(_ spec: FiniteSpec) throws -> pid_t {
    var fileActions: posix_spawn_file_actions_t?
    var attributes: posix_spawnattr_t?
    guard posix_spawn_file_actions_init(&fileActions) == 0 else {
        throw OpError.bad("posix_spawn file actions init failed")
    }
    defer { posix_spawn_file_actions_destroy(&fileActions) }
    guard posix_spawnattr_init(&attributes) == 0 else {
        throw OpError.bad("posix_spawn attributes init failed")
    }
    defer { posix_spawnattr_destroy(&attributes) }
    var defaults = sigset_t()
    sigemptyset(&defaults)
    for signalValue in [SIGHUP, SIGINT, SIGQUIT, SIGTERM, SIGPIPE, SIGALRM, SIGUSR1, SIGUSR2, SIGCHLD] {
        sigaddset(&defaults, signalValue)
    }
    var emptyMask = sigset_t()
    sigemptyset(&emptyMask)
    let flags = Int16(POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK)
    let setup = [
        posix_spawnattr_setflags(&attributes, flags),
        posix_spawnattr_setsigdefault(&attributes, &defaults),
        posix_spawnattr_setsigmask(&attributes, &emptyMask),
        posix_spawn_file_actions_addopen(&fileActions, STDIN_FILENO, "/dev/null", O_RDONLY, 0),
        posix_spawn_file_actions_addinherit_np(&fileActions, STDOUT_FILENO),
        posix_spawn_file_actions_addinherit_np(&fileActions, STDERR_FILENO),
        posix_spawn_file_actions_addchdir_np(&fileActions, spec.cwd),
    ]
    if let failure = setup.first(where: { $0 != 0 }) {
        throw OpError.bad("posix_spawn setup failed: \(String(cString: strerror(failure)))")
    }
    let environment = spec.environment.keys.sorted().map { "\($0)=\(spec.environment[$0]!)" }
    var child: pid_t = 0
    let result = try withCStrings(spec.argv) { argv in
        let executable = argv[0]
        return try withCStrings(environment) { envp in
            posix_spawn(&child, executable, &fileActions, &attributes, argv, envp)
        }
    }
    guard result == 0 else {
        throw OpError.bad("posix_spawn failed: \(String(cString: strerror(result)))")
    }
    return child
}

/// Observe the exit without reaping, then reap under the forwarding lock.
private func waitFiniteExecutor(_ child: pid_t) -> Int32 {
    var info = siginfo_t()
    while waitid(P_PID, id_t(child), &info, WEXITED | WNOWAIT) != 0 {
        if errno != EINTR {
            finiteRefuse(.custodyLost, "waitid failed: \(String(cString: strerror(errno)))")
        }
    }
    finiteLock.lock()
    finiteReaped = true
    finiteLock.unlock()
    var status: Int32 = 0
    while waitpid(child, &status, 0) != child {
        if errno != EINTR {
            finiteRefuse(.custodyLost, "waitpid failed: \(String(cString: strerror(errno)))")
        }
    }
    return status
}

/// Entered from main.swift before `serve()`; never returns.
func runFiniteExecutor(_ arguments: [String]) -> Never {
    let spec: FiniteSpec
    do {
        spec = try parseFiniteSpec(arguments)
    } catch let OpError.bad(message) {
        finiteRefuse(.usage, message)
    } catch {
        finiteRefuse(.usage, "\(error)")
    }
    // launchd's job cleanup is keyed on the job's own process group. A helper
    // started any other way cannot offer that boundary to its executor.
    guard getppid() == 1, getpgrp() == getpid() else {
        finiteRefuse(.notJobLeader, "finite mode requires a launchd job process-group leader")
    }
    installFiniteSignalForwarding()
    finiteLock.lock()
    if finiteInterrupted {
        finiteLock.unlock()
        finiteRefuse(.interruptedBeforeSpawn, "termination requested before executor spawn")
    }
    let child: pid_t
    do {
        child = try spawnFiniteExecutor(spec)
    } catch let OpError.bad(message) {
        finiteLock.unlock()
        finiteRefuse(.spawnFailed, message)
    } catch {
        finiteLock.unlock()
        finiteRefuse(.spawnFailed, "\(error)")
    }
    finiteChild = child
    finiteLock.unlock()
    finiteLog(["helper_pid": Int(getpid()), "executor_pid": Int(child), "pgid": Int(getpgrp())])
    let status = waitFiniteExecutor(child)
    let signalValue = status & 0x7f
    if signalValue == 0 {
        let code = (status >> 8) & 0xff
        finiteLog(["executor_pid": Int(child), "exit_code": Int(code)])
        exit(code == 0 ? FiniteExit.executorSucceeded.rawValue : FiniteExit.executorFailed.rawValue)
    }
    finiteLog(["executor_pid": Int(child), "signal": Int(signalValue)])
    exit(FiniteExit.executorSignaled.rawValue)
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

// A finite release-executor job runs this same signed binary; it must never
// reach desktop registration, the helper socket or the root keeper.
if CommandLine.arguments.count > 1, CommandLine.arguments[1] == finiteExecutorFlag {
    runFiniteExecutor(Array(CommandLine.arguments.dropFirst(2)))
}
serve()
