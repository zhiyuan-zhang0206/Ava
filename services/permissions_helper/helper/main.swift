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
// serial, so a lock would buy nothing.
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
private let inheritedSocketFDEnvironment = "AVA_PERMISSIONS_HELPER_LISTEN_FD"

struct Child {
    let pid: pid_t
    let startedAt: Date
}

private let childTableLock = NSLock()
private var children: [String: Child] = [:]
private var childReaper: DispatchSourceSignal?

func withChildTable<T>(_ body: () -> T) -> T {
    childTableLock.lock()
    defer { childTableLock.unlock() }
    return body()
}

func childIsAlive(_ pid: pid_t) -> Bool {
    kill(pid, 0) == 0
}

func startChildReaper() {
    let source = DispatchSource.makeSignalSource(signal: SIGCHLD)
    source.setEventHandler {
        while true {
            let pid = waitpid(-1, nil, WNOHANG)
            if pid <= 0 { break }
            withChildTable {
                if let name = children.first(where: { $0.value.pid == pid })?.key {
                    children.removeValue(forKey: name)
                }
            }
        }
    }
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
    if let existingPID = withChildTable({
        children[name].flatMap { childIsAlive($0.pid) ? $0.pid : nil }
    }) {
        return ["pid": existingPID, "reused": true]
    }
    try createParentDirectory(of: stdoutPath)
    try createParentDirectory(of: stderrPath)

    var argumentPointers = argv.map { strdup($0) }
    guard argumentPointers.allSatisfy({ $0 != nil }) else {
        argumentPointers.forEach { if let pointer = $0 { free(pointer) } }
        throw OpError.bad("could not allocate argv")
    }
    argumentPointers.append(nil)
    defer { argumentPointers.forEach { if let pointer = $0 { free(pointer) } } }

    var childEnvironment = env
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

    let spawnFlags = Int16(POSIX_SPAWN_SETSID | POSIX_SPAWN_CLOEXEC_DEFAULT)
    let setupResults = [
        posix_spawnattr_setflags(&attributes, spawnFlags),
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
    let pid: pid_t = withChildTable {
        result = argumentPointers.withUnsafeMutableBufferPointer { argvBuffer in
            environmentPointers.withUnsafeMutableBufferPointer { envBuffer in
                posix_spawn(
                    &spawnedPID, argvBuffer[0], &fileActions, &attributes,
                    argvBuffer.baseAddress, envBuffer.baseAddress
                )
            }
        }
        if result == 0 {
            children[name] = Child(pid: spawnedPID, startedAt: Date())
        }
        return spawnedPID
    }
    guard result == 0 else {
        throw OpError.bad("posix_spawn failed: \(String(cString: strerror(result)))")
    }
    return ["pid": pid, "reused": false]
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
    let sessions: [[String: Any]] = withChildTable {
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
    let alive = withChildTable { children[name].map { childIsAlive($0.pid) } ?? false }
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

    let pid: pid_t
    if let name {
        guard let child = withChildTable({ children[name] }) else {
            throw OpError.bad("unknown session: \(name)")
        }
        pid = child.pid
    } else {
        guard let requestedPID, requestedPID > 0, let exactPID = pid_t(exactly: requestedPID) else {
            throw OpError.bad("signal pid must be positive")
        }
        pid = exactPID
    }
    return ["sent": kill(pid, signalValue) == 0]
}

func selfUpgrade(_ req: [String: Any], listeningFD: Int32) throws -> [String: Any] {
    guard let requestedPath = req["exe_path"] as? String,
          (requestedPath as NSString).isAbsolutePath
    else { throw OpError.bad("self_upgrade needs absolute exe_path") }

    let executableURL = URL(fileURLWithPath: requestedPath)
        .standardizedFileURL.resolvingSymlinksInPath()
    let bundleURL = Bundle.main.bundleURL.standardizedFileURL.resolvingSymlinksInPath()
    let bundlePath = bundleURL.path
    guard executableURL.path == bundlePath || executableURL.path.hasPrefix(bundlePath + "/") else {
        throw OpError.bad("self_upgrade exe_path is outside helper bundle")
    }

    var arguments: [UnsafeMutablePointer<CChar>?] = [strdup(executableURL.path), nil]
    guard arguments[0] != nil else { throw OpError.bad("could not allocate self_upgrade argv") }
    defer { free(arguments[0]) }
    guard setenv(inheritedSocketFDEnvironment, String(listeningFD), 1) == 0 else {
        throw OpError.bad("could not preserve listening socket")
    }
    guard setCloseOnExec(listeningFD, false) else {
        unsetenv(inheritedSocketFDEnvironment)
        throw OpError.bad("could not preserve listening socket")
    }

    arguments.withUnsafeMutableBufferPointer { buffer in
        _ = execv(buffer[0], buffer.baseAddress)
    }
    let execError = errno
    _ = setCloseOnExec(listeningFD, true)
    unsetenv(inheritedSocketFDEnvironment)
    throw OpError.bad("self_upgrade exec failed: \(String(cString: strerror(execError)))")
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

// MARK: - Dispatch

func dispatch(_ req: [String: Any], listeningFD: Int32) -> [String: Any] {
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
            result = ["pong": true, "preflight_screen": CGPreflightScreenCaptureAccess(),
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
        case "self_upgrade": result = try selfUpgrade(req, listeningFD: listeningFD)
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

// MARK: - Socket server

func socketPath() -> String {
    if let p = ProcessInfo.processInfo.environment["AVA_PERMISSIONS_HELPER_SOCKET"] { return p }
    if CommandLine.arguments.count > 1 { return CommandLine.arguments[1] }
    FileHandle.standardError.write(Data("AvaPermissionsHelper: no socket path (env AVA_PERMISSIONS_HELPER_SOCKET or argv[1])\n".utf8))
    exit(2)
}

/// Register the helper into the Screen Recording and Accessibility lists (and
/// prompt once if the session allows), so the operator grants by flipping a
/// toggle rather than hunting via the "+" button. No effect once granted.
func registerPermissions() {
    if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
    let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    _ = AXIsProcessTrustedWithOptions(opts)
}

/// Serve desktop requests only through an owner-only Unix socket. `chmod` locks
/// the socket file to mode 0700, and `getpeereid` admits only this process's
/// uid; same-uid processes remain the documented residual threat surface.
func serve() {
    reportResponsiblePID()
    registerPermissions()
    let path = socketPath()
    let fd: Int32
    if let inherited = ProcessInfo.processInfo.environment[inheritedSocketFDEnvironment],
       let inheritedFD = Int32(inherited), inheritedFD >= 0 {
        fd = inheritedFD
        unsetenv(inheritedSocketFDEnvironment)
    } else {
        unlink(path)
        fd = socket(AF_UNIX, SOCK_STREAM, 0)
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
    }
    guard setCloseOnExec(fd, true) else {
        perror("fcntl"); exit(1)
    }
    startChildReaper()
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
                writeJSONLine(conn, dispatch(req, listeningFD: fd))
            } else {
                writeJSONLine(conn, ["id": NSNull(), "ok": false, "error": "JSON parse error"])
            }
        }
        close(conn)
    }
}

serve()
