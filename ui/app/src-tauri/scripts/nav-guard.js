// External-link guard, injected into every page the app loads.
//
// The app is a single window with no tabs, so a link that opens a "new tab"
// has to leave the app entirely. `window.open` and `target="_blank"` clicks
// are intercepted here and handed to the Rust side, which tries the cluster's
// own Chrome before falling back to the system browser.
//
// Same-window navigation is guarded natively (Rust `on_navigation`), not here:
// a page can always defeat a JS-level navigation guard, and the native one runs
// even when this script fails to install.
(function () {
  if (window.__AVA_APP_NAV_GUARD__) return;
  window.__AVA_APP_NAV_GUARD__ = true;

  function openExternal(raw) {
    var href;
    try {
      href = new URL(String(raw), window.location.href).href;
    } catch (err) {
      return;
    }
    window.__TAURI_INTERNALS__
      .invoke("app_open_external", { url: href })
      .catch(function (error) {
        console.error("[ava-app] could not hand off an external link", error);
      });
  }

  // Deny every window.open and route it out: even in-cluster links (Inspector page links are
  // target=_blank) belong in a browser, because the app has nowhere to put a
  // second tab. Returning null is what a blocked popup looks like to callers.
  window.open = function (url) {
    if (url) openExternal(url);
    return null;
  };

  // Capture phase so the page's own handlers cannot swallow the click first.
  document.addEventListener(
    "click",
    function (event) {
      var target = event.target;
      if (!target || typeof target.closest !== "function") return;
      var anchor = target.closest('a[target="_blank"]');
      if (!anchor || !anchor.href) return;
      event.preventDefault();
      openExternal(anchor.href);
    },
    true,
  );
})();
