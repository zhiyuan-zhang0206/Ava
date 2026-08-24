// Android local-notification bridge.
//
// The app subscribes to the SAME stream the console does — the gateway's
// global broadcast `/api/system`, which forwards the cross-agent, low-frequency
// GLOBAL_ROLES for every agent. Riding the page's own credentials is the whole
// point: the webview holds the session cookie, so the bridge needs no second
// auth mechanism and no token handling in Rust.
//
// It runs inside the page for the same reason, which sets its boundary: it is
// live only while the webview is alive. The foreground service keeps the
// process (and therefore the webview) from being reaped while the app is
// backgrounded; with the app closed there are no notifications, which is the
// documented limit — closed-app push needs FCM and is out of scope.
//
// The notified subset is deliberately small: an agent finishing a turn, and an
// agent blocking on a user decision. Everything else the console shows on
// screen; an app that notified more would be noise.
//
// Native code captures notification taps because the notification plugin's JS
// event can be lost on a cold launch. Once this credential-gated SSE opens, the
// bridge consumes that per-tap marker and navigates the same window to Inbox.
(function () {
  if (window.top !== window) return;
  if (window.__AVA_APP_NOTIFY__) return;
  var started = false;
  var sseOpen = false;

  function tryStart() {
    var cfg = window.__AVA_APP__;
    if (started || !cfg || !cfg.notifications || !cfg.gatewayUrl) return;
    started = true;
    window.__AVA_APP_NOTIFY__ = true;

    // Statuses that mean the agent was doing something; the transition out of one
    // of these into `idling` is what "finished" means on the wire.
    var BUSY = { running: true, starting: true, restarting: true };

    // Per agent: the last status seen, and the awaiting-response notice ids
    // already announced. Both start empty, and the first frame for an agent only
    // seeds them — otherwise every reconnect would replay the whole fleet as new
    // news. Keeping the notice ids per agent (not in one global set) is what lets
    // a resolved notice be forgotten without touching another agent's.
    var lastStatus = new Map();
    var announcedNotices = new Map();

    function notify(title, body) {
      window.__TAURI_INTERNALS__
        .invoke("app_notify", { title: title, body: body })
        .catch(function (error) {
          console.error("[ava-app] notification failed", error);
        });
    }

    function consumeDeeplink() {
      if (!started || !sseOpen || !cfg.entryUrl) return;
      var target;
      try {
        target = new URL("fleet#inbox", cfg.entryUrl);
      } catch (error) {
        console.error("[ava-app] could not resolve Inbox deep link", error);
        return;
      }
      window.__TAURI_INTERNALS__
        .invoke("app_take_pending_click")
        .then(function (pending) {
          if (!pending) return;
          window.location.href = target.href;
        })
        .catch(function (error) {
          console.error("[ava-app] notification click check failed", error);
        });
    }

    function agentName(snapshot) {
      return snapshot.label || "Agent #" + snapshot.agent_id;
    }

    function onSnapshot(snapshot) {
      if (!snapshot || typeof snapshot.agent_id !== "number") return;
      var id = snapshot.agent_id;
      var seen = lastStatus.has(id);
      var previous = lastStatus.get(id);
      lastStatus.set(id, snapshot.status);

      // Agent finished: a busy status settled into idling.
      if (seen && BUSY[previous] && snapshot.status === "idling") {
        notify("Ava — " + agentName(snapshot), "Finished its turn.");
      }

      // Agent needs input: a require-response notice that was not open before.
      var awaiting = snapshot.notices_awaiting_response || [];
      var announced = announcedNotices.get(id) || new Set();
      var open = new Set();
      for (var i = 0; i < awaiting.length; i += 1) {
        var notice = awaiting[i];
        if (!notice || typeof notice.id !== "number") continue;
        open.add(notice.id);
        if (announced.has(notice.id)) continue;
        announced.add(notice.id);
        // Seeding pass: adopt what is already open without announcing it.
        if (!seen) continue;
        notify("Ava — " + agentName(snapshot) + " needs input", notice.title || "");
      }
      // Forget this agent's resolved notices so a re-opened one announces again.
      announced.forEach(function (noticeId) {
        if (!open.has(noticeId)) announced.delete(noticeId);
      });
      announcedNotices.set(id, announced);
    }

    function onEvent(event) {
      if (!event || typeof event !== "object") return;
      if (event.role === "agent_updated" || event.role === "agent_spawned") {
        onSnapshot(event.snapshot);
      }
    }

    function connect() {
      var source = new EventSource(cfg.gatewayUrl + "/api/system", { withCredentials: true });
      source.onopen = function () {
        sseOpen = true;
        consumeDeeplink();
      };
      source.onmessage = function (message) {
        var parsed;
        try {
          parsed = JSON.parse(message.data);
        } catch (err) {
          return;
        }
        // `/api/system` sends one event per frame, but the batched array shape of
        // the sibling `/api/system/all` stream is cheap to tolerate.
        if (Array.isArray(parsed)) parsed.forEach(onEvent);
        else onEvent(parsed);
      };
      source.onerror = function () {
        sseOpen = false;
        // EventSource retries a dropped connection itself; only a CLOSED socket
        // is a dead end that needs a fresh one.
        if (source.readyState !== EventSource.CLOSED) return;
        source.close();
        window.setTimeout(connect, 5000);
      };
    }

    connect();
    window.addEventListener("focus", consumeDeeplink);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) consumeDeeplink();
    });
  }

  tryStart();
  if (!started) {
    window.addEventListener("ava-app-config", function onConfig() {
      window.removeEventListener("ava-app-config", onConfig);
      tryStart();
    });
  }
})();
