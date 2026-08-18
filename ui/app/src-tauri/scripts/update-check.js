// Android update check.
//
// `tauri-plugin-updater` is desktop-only — there is no in-place APK swap — so
// Android checks the GitHub Releases API for a newer `shell-v*` tag and, when
// it finds one, offers the APK. Delivery is a browser download the user
// confirms, not a silent install.
//
// It runs in the page (rather than in Rust) so the check costs the binary no
// HTTP stack and no TLS root store on a target that cannot be built or tested
// on this machine. The console's CSP already allows `connect-src https:`, and
// the GitHub API answers cross-origin.
(function () {
  var cfg = window.__AVA_SHELL__;
  if (!cfg || !cfg.releasesApi || !cfg.version) return;
  if (window.top !== window) return;
  var MARKER = "ava-shell-update-checked";
  if (!window.sessionStorage || window.sessionStorage.getItem(MARKER)) return;
  window.sessionStorage.setItem(MARKER, "1");

  var TAG_PREFIX = "shell-v";

  /** Compare dotted numeric versions; >0 when `a` is newer than `b`. */
  function compare(a, b) {
    var left = String(a).split(".").map(Number);
    var right = String(b).split(".").map(Number);
    for (var i = 0; i < Math.max(left.length, right.length); i += 1) {
      var l = left[i] || 0;
      var r = right[i] || 0;
      if (l !== r) return l - r;
    }
    return 0;
  }

  function banner(version, assetUrl) {
    var bar = document.createElement("div");
    bar.setAttribute("data-ava-shell", "update-banner");
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
    text.textContent = "Ava " + version + " is available";

    var actions = document.createElement("span");
    actions.style.cssText = "display:flex;gap:8px;flex:none";

    var download = document.createElement("button");
    download.type = "button";
    download.textContent = "Download";
    download.style.cssText =
      "border:0;border-radius:6px;padding:6px 12px;background:#4f46e5;color:#fff;font:inherit";
    download.addEventListener("click", function () {
      window.__TAURI_INTERNALS__
        .invoke("shell_open_external", { url: assetUrl })
        .catch(function (error) {
          console.error("[ava-shell] update download handoff failed", error);
        });
      bar.remove();
    });

    var dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.textContent = "Later";
    dismiss.style.cssText =
      "border:0;border-radius:6px;padding:6px 12px;background:transparent;color:#a1a1aa;font:inherit";
    dismiss.addEventListener("click", function () {
      bar.remove();
    });

    actions.appendChild(download);
    actions.appendChild(dismiss);
    bar.appendChild(text);
    bar.appendChild(actions);
    document.body.appendChild(bar);
  }

  (async function () {
    var releases;
    try {
      var response = await fetch(cfg.releasesApi, {
        headers: { Accept: "application/vnd.github+json" },
      });
      if (!response.ok) return;
      releases = await response.json();
    } catch (err) {
      // Offline or rate-limited: the check is best-effort, never a nag.
      return;
    }
    if (!Array.isArray(releases)) return;

    // GitHub orders releases by creation time, which is not version order: a
    // later backfill can appear before a newer shell. Scan the whole response
    // and choose the highest eligible semver that actually has an APK.
    var candidate = null;
    for (var i = 0; i < releases.length; i += 1) {
      var release = releases[i];
      if (!release || release.draft || release.prerelease) continue;
      var tag = String(release.tag_name || "");
      if (!/^shell-v\d+\.\d+\.\d+$/.test(tag)) continue;
      var version = tag.slice(TAG_PREFIX.length);
      if (compare(version, cfg.version) <= 0) continue;
      var assets = release.assets || [];
      for (var j = 0; j < assets.length; j += 1) {
        if (/\.apk$/i.test(assets[j].name || "")) {
          if (!candidate || compare(version, candidate.version) > 0) {
            candidate = {
              version: version,
              assetUrl: assets[j].browser_download_url,
            };
          }
          break;
        }
      }
    }
    if (!candidate) return;
    if (cfg.notifications) {
      window.__TAURI_INTERNALS__
        .invoke("shell_notify", {
          title: "Ava " + candidate.version + " is available",
          body: "Tap the download bar in the app to install it.",
        })
        .catch(function (error) {
          console.error("[ava-shell] update notification failed", error);
        });
    }
    if (document.body) banner(candidate.version, candidate.assetUrl);
  })();
})();
