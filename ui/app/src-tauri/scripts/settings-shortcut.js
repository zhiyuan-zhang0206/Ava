// Android has no tray menu, so the remote console needs one narrow route back
// to the bundled shell settings. The button can only navigate there: the
// remote origin has no permission to persist or read settings directly.
(function () {
  if (window.top !== window) return;
  if (location.protocol === "tauri:" || location.hostname === "tauri.localhost") return;

  window.addEventListener("DOMContentLoaded", function () {
    if (document.querySelector('[data-ava-shell="settings"]')) return;
    var button = document.createElement("button");
    button.type = "button";
    button.setAttribute("data-ava-shell", "settings");
    button.setAttribute("aria-label", "Ava shell settings");
    button.textContent = "⚙";
    button.style.cssText = [
      "position:fixed",
      "right:12px",
      "bottom:12px",
      "z-index:2147483646",
      "width:38px",
      "height:38px",
      "border:1px solid rgba(255,255,255,.16)",
      "border-radius:19px",
      "background:rgba(24,24,27,.88)",
      "color:#d4d4d8",
      "font:20px/1 system-ui,sans-serif",
      "box-shadow:0 2px 10px rgba(0,0,0,.3)",
    ].join(";");
    button.addEventListener("click", function () {
      window.__TAURI_INTERNALS__.invoke("shell_open_settings").catch(function (error) {
        console.error("Ava shell could not open settings", error);
      });
    });
    document.body.appendChild(button);
  });
})();
