// Appended to the production child/keeper source by test_ava_root_custody.py.
// The test inserts barriers at existing native calls; production has no hooks.
let stopEntered = DispatchSemaphore(value: 0)
let stopRelease = DispatchSemaphore(value: 0)
let sessionSignalEntered = DispatchSemaphore(value: 0)
let sessionSignalRelease = DispatchSemaphore(value: 0)
let nativeReapCompleted = DispatchSemaphore(value: 0)
let shutdownEntered = DispatchSemaphore(value: 0)
let shutdownRelease = DispatchSemaphore(value: 0)
let fixtureMode = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "custody"

func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw OpError.bad(message) }
}

func pausedRootSignal(_ pid: pid_t, _ signalValue: Int32) -> Int32 {
    stopEntered.signal()
    guard stopRelease.wait(timeout: .now() + 5) == .success else { return -1 }
    return Darwin.kill(pid, signalValue)
}

func observedWaitpid(_ pid: pid_t, _ status: UnsafeMutablePointer<Int32>?, _ flags: Int32) -> pid_t {
    let result = Darwin.waitpid(pid, status, flags)
    if result > 0 { nativeReapCompleted.signal() }
    return result
}

func pausedSessionSignal(_ pid: pid_t, _ signalValue: Int32) -> Int32 {
    sessionSignalEntered.signal()
    guard sessionSignalRelease.wait(timeout: .now() + 5) == .success else { return -1 }
    return Darwin.kill(pid, signalValue)
}

func pausedHelperStopIntent(_ runDir: String) throws {
    if fixtureMode == "custody" {
        shutdownEntered.signal()
        guard shutdownRelease.wait(timeout: .now() + 5) == .success else {
            throw OpError.bad("shutdown intent barrier timed out")
        }
    }
    try StopIntent.store(runDir, owner: .helper)
}

func expectRefusal(_ body: () throws -> Void) throws {
    do { try body() }
    catch { return }
    throw OpError.bad("unsafe helper shutdown/admission was accepted")
}

func startReaping() -> DispatchSemaphore {
    let started = DispatchSemaphore(value: 0)
    let finished = DispatchSemaphore(value: 0)
    DispatchQueue.global().async {
        started.signal()
        reapExitedChildren()
        finished.signal()
    }
    precondition(started.wait(timeout: .now() + 5) == .success)
    return finished
}

func exitWithoutReaping(_ pid: pid_t) throws {
    try require(Darwin.kill(pid, SIGKILL) == 0, "fixture child did not exit")
    var info = siginfo_t()
    // WNOWAIT observes a real exited child while retaining its PID. A sleep
    // here would let the reaper run too early and make the regression flaky.
    try require(waitid(P_PID, id_t(pid), &info, WEXITED | WNOWAIT) == 0,
                "could not observe the fixture child's retained exit")
}

func runCustodyTests() throws {
    let directory = CommandLine.arguments[1]
    setenv("AVA_PERMISSIONS_HELPER_ROOT_SEED", directory + "/seed.json", 1)
    if fixtureMode == "crash-after-intent" {
        _ = try requestHelperShutdown(runDir: directory)
        _exit(73)
    }
    if fixtureMode == "restart-after-intent" {
        let retained = try helperShouldExitBeforeStart()
        try require(retained, "restart lost durable helper stop")
        try require(rootKeeper.status()["state"] as? String == "unseeded", "startup ran root")
        print("helper stopped before native startup")
        return
    }
    let seed = RootSeed(
        argv: ["/bin/sleep", "5"], cwd: directory, runDir: directory,
        stdoutPath: directory + "/root.out", stderrPath: directory + "/root.err",
        environment: [:]
    )
    try rootKeeper.configure(seed)
    let spawnDeadline = Date().addingTimeInterval(5)
    while rootKeeper.status()["pid"] == nil && Date() < spawnDeadline { usleep(1_000) }
    guard let rootPID = rootKeeper.status()["pid"] as? Int else {
        throw OpError.bad("root fixture did not spawn")
    }
    try expectRefusal { _ = try requestHelperShutdown(runDir: directory) }
    let stopped = DispatchSemaphore(value: 0)
    DispatchQueue.global().async {
        do { _ = try rootKeeper.requestStop() }
        catch { FileHandle.standardError.write(Data("stop error: \(error)\n".utf8)) }
        stopped.signal()
    }
    try require(stopEntered.wait(timeout: .now() + 5) == .success, "stop did not enter barrier")
    try exitWithoutReaping(pid_t(rootPID))
    let rootReaped = startReaping()
    // waitpid must not free the PID while root_stop still owns its snapshot.
    try require(nativeReapCompleted.wait(timeout: .now() + 0.2) == .timedOut,
                "native waitpid escaped root signal ownership boundary")
    stopRelease.signal()
    try require(stopped.wait(timeout: .now() + 5) == .success, "root stop deadlocked")
    try require(rootReaped.wait(timeout: .now() + 5) == .success, "root reap deadlocked")
    try require(nativeReapCompleted.wait(timeout: .now() + 5) == .success, "root was not reaped")
    try require(rootKeeper.status()["state"] as? String == "stopped", "root exit lost stop intent")
    try require(rootKeeper.status()["pid"] == nil, "reaped root retained a signalable PID")
    try require(rootKeeper.status()["restarts"] as? Int == 0, "stopped root was restarted")

    let session = try spawnProcess([
        "name": "fixture-session", "argv": ["/bin/sleep", "5"], "cwd": directory,
        "env": [String: String](), "stdout": directory + "/session.out",
        "stderr": directory + "/session.err",
    ])
    guard let sessionPID = session["pid"] as? pid_t else { throw OpError.bad("session pid missing") }
    try expectRefusal { _ = try requestHelperShutdown(runDir: directory) }
    let signalled = DispatchSemaphore(value: 0)
    DispatchQueue.global().async {
        do { _ = try signalSession(["name": "fixture-session", "sig": Int(SIGTERM)]) }
        catch { FileHandle.standardError.write(Data("session signal error: \(error)\n".utf8)) }
        signalled.signal()
    }
    try require(sessionSignalEntered.wait(timeout: .now() + 5) == .success, "session signal absent")
    try exitWithoutReaping(sessionPID)
    let sessionReaped = startReaping()
    try require(nativeReapCompleted.wait(timeout: .now() + 0.2) == .timedOut,
                "native waitpid escaped named session signal ownership boundary")
    sessionSignalRelease.signal()
    try require(signalled.wait(timeout: .now() + 5) == .success, "session signal deadlocked")
    try require(sessionReaped.wait(timeout: .now() + 5) == .success, "session reap deadlocked")
    try require(nativeReapCompleted.wait(timeout: .now() + 5) == .success, "session was not reaped")
    let sessionAlive = try sessionHas(["name": "fixture-session"])["alive"] as? Bool
    try require(sessionAlive == false,
                "reaped session retained a signalable PID")

    // A direct child outside both owner tables belongs to another native owner
    // (Foundation Process in the helper). The helper reaper must leave it alone.
    let other = try spawnDetachedChild(
        argv: ["/bin/sh", "-c", "exit 37"], environment: [:], cwd: directory,
        stdoutPath: directory + "/other.out", stderrPath: directory + "/other.err"
    )
    usleep(100_000)
    reapExitedChildren()
    var status: Int32 = 0
    try require(Darwin.waitpid(other, &status, 0) == other, "helper stole another owner's child")
    try require(status == 37 << 8, "other native owner lost its exit status")
    print("root and named-session ownership, native exits, and foreign-child isolation passed")

    try expectRefusal { _ = try requestHelperShutdown(runDir: directory + "/foreign") }
    let shutdownAccepted = DispatchSemaphore(value: 0)
    DispatchQueue.global().async {
        if let reply = try? requestHelperShutdown(runDir: directory),
           reply["stopping"] as? Bool == true { shutdownAccepted.signal() }
    }
    try require(shutdownEntered.wait(timeout: .now() + 5) == .success, "shutdown did not enter")
    let spawnRejected = DispatchSemaphore(value: 0)
    let seedRejected = DispatchSemaphore(value: 0)
    DispatchQueue.global().async {
        do {
            _ = try spawnProcess([
                "name": "late", "argv": ["/bin/sleep", "0"], "env": [String: String](),
                "cwd": directory, "stdout": directory + "/late.out", "stderr": directory + "/late.err",
            ])
        } catch let OpError.bad(message) {
            if message == "helper retirement has closed admission" { spawnRejected.signal() }
        } catch {}
    }
    DispatchQueue.global().async {
        do { try rootKeeper.configure(seed) }
        catch let OpError.bad(message) {
            if message == "helper admission is closed or root seed home differs" { seedRejected.signal() }
        } catch {}
    }
    try require(spawnRejected.wait(timeout: .now() + 0.1) == .timedOut, "spawn escaped shutdown lock")
    try require(seedRejected.wait(timeout: .now() + 0.1) == .timedOut, "seed escaped shutdown lock")
    shutdownRelease.signal()
    try require(shutdownAccepted.wait(timeout: .now() + 5) == .success, "shutdown was not acknowledged")
    try require(spawnRejected.wait(timeout: .now() + 5) == .success, "spawn admitted after shutdown")
    try require(seedRejected.wait(timeout: .now() + 5) == .success, "seed admitted after shutdown")
    let retained = try helperShouldExitBeforeStart()
    try require(retained, "shutdown did not persist helper intent")
    try require(rootKeeper.status()["pid"] == nil, "shutdown spawned a root")
    print("helper shutdown admission passed")
}

do { try runCustodyTests() }
catch {
    FileHandle.standardError.write(Data("native child custody failed: \(error)\n".utf8))
    exit(1)
}
