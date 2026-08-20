"use client";

// Applies the selected plugin skin to the root element — the render half of
// `contributions.ui.themes`.
//
// Renders nothing. The declared tokens are written as inline custom properties
// on <html>, which is the same layer next-themes' light/dark class switch
// targets, one step more specific: an inline property wins over both the
// `:root` and `.dark` rules, so a pack applies over whichever mode is active
// and the tokens it does NOT name keep following that mode.
//
// That specificity is exactly why a pack needs a dark half. Winning over both
// `:root` AND `.dark` means a single flat map pins every color it sets across
// both modes — the mode toggle keeps flipping, and for those colors nothing
// happens. So a pack that declares `darkTokens` gets the half matching the
// RESOLVED mode, and the skin and the mode stay orthogonal; a pack that omits
// it has declared that it means to pin both, and the picker says so.
//
// The tokens come back off the wire already validated as color literals
// against a closed vocabulary (`shared/plugin_ui_contributions.py`), so there
// is nothing to sanitize here — an unknown token or a `var(...)` value could
// not have reached a manifest that loads.

import { useTheme } from "next-themes";
import { useEffect } from "react";

import { useThemePacks, themePackId } from "@/lib/use-theme-packs";
import { useUserSettings } from "@/lib/use-user-settings";

export function ThemePackTokens() {
  const { packs } = useThemePacks();
  const { settings } = useUserSettings();
  const selected = settings["display.theme_pack"] as string | null;
  // A stored id whose plugin was since disabled or uninstalled resolves to no
  // pack: the console falls back to its own palette rather than to whatever
  // else is installed, and the picker shows Default.
  const pack = packs.find((p) => themePackId(p) === selected) ?? null;

  // resolvedTheme, not theme: "system" has to become the concrete mode before
  // it can choose a half. Undefined on the first client render (next-themes
  // has not resolved yet), which reads as light — the same default the
  // stylesheet itself starts from.
  const { resolvedTheme } = useTheme();
  const active =
    pack && resolvedTheme === "dark" && pack.dark_tokens ? pack.dark_tokens : pack?.tokens;

  // Serialized so the effect keys on the token VALUES: a refetch hands back
  // structurally identical objects with new identities, and re-running on
  // those would clear and re-set every property for nothing. Keying on the
  // values also makes the light/dark swap fall out for free — the two halves
  // serialize differently, so flipping mode re-runs the effect, whose cleanup
  // removes the outgoing half before the incoming one is set.
  const tokensJson = active ? JSON.stringify(active) : "";

  useEffect(() => {
    if (!tokensJson) return;
    const root = document.documentElement;
    const tokens = Object.entries(JSON.parse(tokensJson) as Record<string, string>);
    for (const [token, value] of tokens) root.style.setProperty(token, value);
    // Removing on cleanup is what makes switching packs and clearing the
    // choice both work: the previous pack's properties go before the next
    // pack's arrive, so a token only the old pack named does not linger.
    return () => {
      for (const [token] of tokens) root.style.removeProperty(token);
    };
  }, [tokensJson]);

  return null;
}
