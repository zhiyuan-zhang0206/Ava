// Resolve the colour theme before first paint — inlined into the <head> of
// every gate page by `Gate.__init__` (the `__THEME_JS__` marker).
//
// The app drives dark mode with next-themes (`attribute="class"`,
// `defaultTheme="system"`), which stores the user's choice under the
// localStorage key "theme" as "light" | "dark" | "system" and lands the
// RESOLVED value as a class on <html>. The gate shares an origin with the app
// (it owns the entry port and proxies the app behind it), so reading that same
// key is what keeps a user's chosen theme from flipping when a rollout swaps
// the app out for this page. Anything else — no choice stored, "system",
// or a value we do not recognise — follows the OS, and keeps following it
// while the page sits open.
(function () {
  var root = document.documentElement;
  var media = window.matchMedia("(prefers-color-scheme: dark)");

  function stored() {
    try {
      var value = localStorage.getItem("theme");
      return value === "light" || value === "dark" ? value : null;
    } catch (e) {
      // Storage can be blocked outright (private mode, cookies off).
      return null;
    }
  }

  function apply() {
    var theme = stored() || (media.matches ? "dark" : "light");
    root.classList.remove("light", "dark");
    root.classList.add(theme);
  }

  apply();
  media.addEventListener("change", apply);
})();
