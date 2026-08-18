// Deterministic in-page QA probes for the ava-qa-inspection skill.
//
// Inject this into the page under audit via ava.mcps.chrome's evaluate tool.
// It returns a JSON-serializable object: one array per check, each entry a
// candidate defect with a selector and the measured numbers. These are
// *evidence*, not verdicts — the agent confirms each is a real defect (some
// overflow / repetition is intentional). See reference/checklist.md for the
// probe → defect-class map.
//
// Written as a bare IIFE expression so evaluate returns its value directly.
// Read-only: it measures layout, never mutates the page.

(() => {
  const CAP = 15; // max entries per check — keep the report readable
  const round = (n) => Math.round(n * 10) / 10;

  // Short-ish selector: nearest id anchor, then tag + nth-of-type up to it.
  const selectorFor = (el) => {
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 5; depth++) {
      if (node.id) {
        parts.unshift(`#${node.id}`);
        break;
      }
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (!parent) {
        parts.unshift(tag);
        break;
      }
      const sibs = [...parent.children].filter((c) => c.tagName === node.tagName);
      const idx = sibs.indexOf(node) + 1;
      parts.unshift(sibs.length > 1 ? `${tag}:nth-of-type(${idx})` : tag);
      node = parent;
    }
    return parts.join(" > ");
  };

  const style = (el) => getComputedStyle(el);
  const rect = (el) => el.getBoundingClientRect();

  const isVisible = (el) => {
    const s = style(el);
    if (s.display === "none" || s.visibility === "hidden" || +s.opacity === 0) return false;
    const r = rect(el);
    return r.width > 0 && r.height > 0;
  };

  const accessibleName = (el) =>
    (el.getAttribute("aria-label") || el.textContent || "").replace(/\s+/g, " ").trim();

  const vw = window.innerWidth;
  const all = [...document.querySelectorAll("body *")];

  const checks = {};

  // 1. overflow — horizontal scrollbar or clipped content the layout shouldn't have.
  checks.overflow = (() => {
    const out = [];
    const scroller = document.scrollingElement || document.documentElement;
    if (scroller.scrollWidth > scroller.clientWidth + 1) {
      out.push({
        selector: "document",
        kind: "page-horizontal-scroll",
        overflowPx: round(scroller.scrollWidth - scroller.clientWidth),
      });
    }
    for (const el of all) {
      const over = el.scrollWidth - el.clientWidth;
      if (over <= 1 || !isVisible(el)) continue;
      const ox = style(el).overflowX;
      // auto/scroll means the scrollbar is intended; visible/hidden means the
      // content is bleeding or being clipped without a way to reach it.
      const intended = ox === "auto" || ox === "scroll";
      out.push({
        selector: selectorFor(el),
        kind: intended ? "scrollbar" : ox === "hidden" ? "clipped" : "bleeding",
        overflowPx: round(over),
        clips: ox === "hidden" && style(el).textOverflow !== "ellipsis",
      });
    }
    return out.sort((a, b) => b.overflowPx - a.overflowPx).slice(0, CAP);
  })();

  // 2. emptyBox — a styled box (border/background) with no content or children.
  //
  // Two intentionally childless, styled shapes get excluded before the
  // border/background test even runs, since both are real UI, not defects:
  //   - a Switch's thumb (`data-slot="switch-thumb"`, see ui/switch.tsx) —
  //     a colored circle with no text by design.
  //   - a progress-bar fill segment — a track (`overflow:hidden`, optionally
  //     `role="progressbar"`) containing a child sized via an inline
  //     percentage `style.width` (data-driven, not a CSS class) rather than
  //     text content. See metrics/primitives.tsx Bar and
  //     context-breakdown.tsx's stacked segments.
  const isSwitchThumb = (el) => el.getAttribute("data-slot") === "switch-thumb";
  const hasProgressTrackAncestor = (el, maxDepth = 3) => {
    let node = el.parentElement;
    for (let d = 0; node && d < maxDepth; d++, node = node.parentElement) {
      if (node.getAttribute("role") === "progressbar") return true;
      const s = style(node);
      if (s.overflowX === "hidden" || s.overflow === "hidden") return true;
    }
    return false;
  };
  const isProgressFill = (el) =>
    /%$/.test(el.style.width || "") && hasProgressTrackAncestor(el);
  checks.emptyBox = (() => {
    const skip = new Set(["HR", "IMG", "SVG", "INPUT", "CANVAS", "VIDEO", "IFRAME", "BR"]);
    const out = [];
    for (const el of all) {
      if (skip.has(el.tagName) || !isVisible(el)) continue;
      if (isSwitchThumb(el) || isProgressFill(el)) continue;
      const r = rect(el);
      if (r.width * r.height < 200) continue;
      if (el.textContent.trim() !== "") continue;
      const hasVisibleChild = [...el.children].some(isVisible);
      if (hasVisibleChild) continue;
      const s = style(el);
      const hasBorder = ["Top", "Right", "Bottom", "Left"].some(
        (side) => parseFloat(s[`border${side}Width`]) > 0 && s[`border${side}Style`] !== "none",
      );
      const bg = s.backgroundColor;
      const hasBg = bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent";
      if (!hasBorder && !hasBg && s.backgroundImage === "none") continue;
      out.push({
        selector: selectorFor(el),
        rect: { w: round(r.width), h: round(r.height) },
        border: hasBorder,
        background: hasBg ? bg : s.backgroundImage !== "none" ? "image" : null,
      });
    }
    return out.slice(0, CAP);
  })();

  // 3. edgeMisalignment — list-like siblings whose left/right edges don't line up.
  //
  // A shared left/right edge is only a meaningful expectation for a
  // vertically stacked list. Three sibling shapes are structurally *not*
  // that, and were the whole of the false-positive yield: inline badge rows
  // (`flex flex-wrap`, e.g. the /control config-item badge line), a table's
  // `<th>` header row, and a single icon's internal `<path>` siblings —
  // none of these are meant to share a column.
  checks.edgeMisalignment = (() => {
    const out = [];
    const seen = new Set();
    for (const el of all) {
      const parent = el.parentElement;
      if (!parent || seen.has(parent)) continue;
      seen.add(parent);
      // Icon internals (multiple <path>/<circle>/... in one <svg>) aren't a
      // layout list at all.
      if (parent.namespaceURI === "http://www.w3.org/2000/svg") continue;
      const kids = [...parent.children].filter((c) => c.tagName === el.tagName && isVisible(c));
      if (kids.length < 3) continue;
      // Require a real top-to-bottom list: each kid must start strictly
      // below the previous one. A horizontal row (flex badges, a table
      // header's <th> cells) has kids sharing ~the same top and fails this,
      // so it's skipped before edges are ever compared.
      const tops = kids.map((c) => Math.round(rect(c).top));
      const stacked = tops.every((t, i) => i === 0 || t > tops[i - 1] + 1);
      if (!stacked) continue;
      for (const edge of ["left", "right"]) {
        const vals = kids.map((c) => Math.round(rect(c)[edge]));
        const freq = {};
        for (const v of vals) freq[v] = (freq[v] || 0) + 1;
        const mode = +Object.entries(freq).sort((a, b) => b[1] - a[1])[0][0];
        const off = kids.filter((c) => Math.abs(rect(c)[edge] - mode) > 2);
        if (off.length && off.length < kids.length) {
          out.push({
            selector: selectorFor(parent),
            edge,
            expected: mode,
            offenders: off.slice(0, 4).map((c) => ({
              selector: selectorFor(c),
              value: round(rect(c)[edge]),
              deltaPx: round(rect(c)[edge] - mode),
            })),
          });
        }
      }
      if (out.length >= CAP) break;
    }
    return out.slice(0, CAP);
  })();

  // 4. duplicateControl — visible controls sharing an accessible name.
  checks.duplicateControl = (() => {
    const controls = all.filter(
      (el) =>
        isVisible(el) &&
        (el.tagName === "BUTTON" ||
          el.getAttribute("role") === "button" ||
          (el.tagName === "A" && el.hasAttribute("href"))),
    );
    const byName = {};
    for (const el of controls) {
      const name = accessibleName(el);
      if (!name || name.length > 40) continue;
      (byName[name] ||= []).push(el);
    }
    return Object.entries(byName)
      .filter(([, els]) => els.length > 1)
      .map(([name, els]) => ({
        name,
        count: els.length,
        selectors: els.slice(0, 4).map(selectorFor),
      }))
      .slice(0, CAP);
  })();

  // 5. offCanvas — visible element bleeding past a viewport edge (not full-bleed).
  //
  // An element whose rect exceeds the viewport but sits inside an ancestor
  // with its own horizontal scrollbar (`overflow-x: auto/scroll/hidden`,
  // e.g. a table wrapped `<div class="overflow-x-auto">`) isn't off-canvas —
  // it's reachable by scrolling that container, and `document.scrollingElement`
  // never grows past the viewport. Only flag bleed that isn't contained by
  // some scrollable ancestor.
  const hasHorizontalScrollAncestor = (el) => {
    for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
      const ox = style(node).overflowX;
      if (ox === "auto" || ox === "scroll" || ox === "hidden") return true;
    }
    return false;
  };
  checks.offCanvas = (() => {
    const out = [];
    for (const el of all) {
      if (!isVisible(el)) continue;
      if (hasHorizontalScrollAncestor(el)) continue;
      const r = rect(el);
      if (r.width >= vw) continue; // intentional full-bleed row
      const overRight = r.right - vw;
      const overLeft = -r.left;
      if (overRight > 1 || overLeft > 1) {
        out.push({
          selector: selectorFor(el),
          edge: overRight > overLeft ? "right" : "left",
          offByPx: round(Math.max(overRight, overLeft)),
          rect: { left: round(r.left), right: round(r.right) },
        });
      }
    }
    return out.sort((a, b) => b.offByPx - a.offByPx).slice(0, CAP);
  })();

  return {
    url: location.href,
    viewport: { w: vw, h: window.innerHeight },
    counts: Object.fromEntries(Object.entries(checks).map(([k, v]) => [k, v.length])),
    ...checks,
  };
})();
