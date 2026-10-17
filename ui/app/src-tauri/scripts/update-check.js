// meiva:ref ui/app/docs/android-updates.meiva.okf.md#android-update-prompt
(function () {
  var cfg = window.__MEIVA_APP__;
  if (!cfg || cfg.platform !== "android" || !cfg.version) return;
  if (window.top !== window) return;
  if (location.protocol === "tauri:" || location.hostname === "tauri.localhost") return;
  var MARKER = "meiva-app-update-checked";
  if (!window.sessionStorage || window.sessionStorage.getItem(MARKER)) return;
  window.sessionStorage.setItem(MARKER, "1");

  function invoke(command, args) {
    return window.__TAURI_INTERNALS__.invoke(command, args);
  }

  function label(check) {
    return "Meiva " + check.latestVersionName + " (build " + check.latestVersionCode + ") is available";
  }

  function banner(check) {
    var bar = document.createElement("div");
    bar.setAttribute("data-meiva-app", "update-banner");
    bar.style.cssText = [
      "position:fixed",
      "left:0",
      "right:0",
      "bottom:0",
      "z-index:2147483647",
      "display:flex",
      "gap:12px",
      "align-items:center",
      "justify-content:space-between",
      "padding:10px 14px",
      "font:14px system-ui,sans-serif",
      "background:#1b1b22",
      "color:#f4f4f5",
      "box-shadow:0 -2px 12px rgba(0,0,0,.35)",
    ].join(";");

    var text = document.createElement("span");
    text.textContent = label(check);

    var actions = document.createElement("span");
    actions.style.cssText = "display:flex;gap:8px;flex:none";

    var update = document.createElement("button");
    update.type = "button";
    update.textContent = "Update";
    update.style.cssText =
      "border:0;border-radius:6px;padding:6px 12px;background:#4f46e5;color:#fff;font:inherit";
    update.addEventListener("click", function () {
      update.disabled = true;
      update.textContent = "Updating…";
      invoke("app_install_update")
        .then(function (outcome) {
          if (outcome && outcome.status !== "failed") {
            bar.remove();
            return;
          }
          var page = outcome && outcome.downloadPage;
          if (!page) {
            bar.remove();
            return;
          }
          update.disabled = false;
          update.textContent = "Open download page";
          update.addEventListener("click", function () {
            invoke("app_open_external", { url: page }).catch(function (error) {
              console.error("[meiva-app] update download page failed", error);
            });
          });
        })
        .catch(function (error) {
          console.error("[meiva-app] update install failed", error);
          bar.remove();
        });
    });

    var dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.textContent = "Later";
    dismiss.style.cssText =
      "border:0;border-radius:6px;padding:6px 12px;background:transparent;color:#a1a1aa;font:inherit";
    dismiss.addEventListener("click", function () {
      bar.remove();
    });

    actions.appendChild(update);
    actions.appendChild(dismiss);
    bar.appendChild(text);
    bar.appendChild(actions);
    document.body.appendChild(bar);
  }

  invoke("app_check_update")
    .then(function (check) {
      if (!check || check.status !== "available") return;
      if (cfg.notifications) {
        invoke("app_notify", {
          title: label(check),
          body: "Tap Update in the app to install it.",
        }).catch(function (error) {
          console.error("[meiva-app] update notification failed", error);
        });
      }
      if (document.body) banner(check);
    })
    .catch(function (error) {
      console.error("[meiva-app] update check failed", error);
    });
})();
