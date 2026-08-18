// Shared clipboard-copy helper for CopyButton (components/copy-button.tsx,
// timeline/buttons.tsx). Prefers the async Clipboard API; falls back to
// `document.execCommand("copy")` via a hidden textarea when
// `navigator.clipboard` is unavailable — it is undefined in non-secure
// contexts (e.g. plain-HTTP LAN deployments), even though lib.dom.d.ts types
// it as always-defined.

/** Copy `text` to the clipboard. Returns whether it succeeded; never throws. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- lib.dom.d.ts is wrong for non-secure contexts
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      // Plain-HTTP LAN: clipboard API unavailable — execCommand via textarea selection
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      // eslint-disable-next-line @typescript-eslint/no-deprecated -- execCommand is the only copy path on plain-HTTP LAN (no secure context); modern secure contexts use the navigator.clipboard branch above.
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    return true;
  } catch {
    return false;
  }
}
