// Desktop auto-login, injected only when a local cluster secret can exist and
// the setting is on.
//
// Why it runs in the page rather than in Rust: the session cookie must end up
// in the webview's own cookie store, set by the browser stack from a real
// `Set-Cookie` on a request the webview made. The webview does the login itself,
// exactly as the console's own login
// form would, so there is no second cookie mechanism to go wrong.
//
// The requests go to the gateway origin with `credentials: "include"`, which is
// how the console's own `api.ts` talks to it (gate and gateway are the same
// host on different ports, and the cookie is host-only, so it is shared).
(function () {
  var cfg = window.__AVA_SHELL__;
  if (!cfg || !cfg.autoLogin || !cfg.gatewayUrl) return;
  // Sub-frames share the session; one attempt per document is enough.
  if (window.top !== window) return;
  // One attempt per page load, and never a second attempt after a reload we
  // ourselves triggered — a gateway that accepts the login but does not set a
  // usable cookie must degrade to the login page, not to a reload loop.
  var MARKER = "ava-shell-auto-login-attempted";
  if (window.sessionStorage && window.sessionStorage.getItem(MARKER)) return;

  function json(url, init) {
    return fetch(url, Object.assign({ credentials: "include" }, init || {}));
  }

  (async function () {
    try {
      var check = await json(cfg.gatewayUrl + "/api/auth/check");
      if (check.ok) {
        var body = await check.json();
        if (body && body.authenticated === true) return;
      }
    } catch (err) {
      // Gateway unreachable — the retry watchdog owns that failure mode.
      return;
    }

    var secret = null;
    try {
      secret = await window.__TAURI_INTERNALS__.invoke("shell_cluster_secret");
    } catch (err) {
      // The command is only registered on desktop with auto-login on; a
      // rejection means "not available here", which is a normal outcome.
      return;
    }
    // No local cluster on this machine: fall through to the login page.
    if (!secret) return;

    try {
      if (window.sessionStorage) window.sessionStorage.setItem(MARKER, "1");
      var login = await json(cfg.gatewayUrl + "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: secret }),
      });
      if (!login.ok) return;
      // The cookie is now in the webview's store; reload so the console boots
      // authenticated instead of rendering its login page.
      window.location.reload();
    } catch (err) {
      // Never surface the secret or throw into the page.
    }
  })();
})();
