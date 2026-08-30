// Memory graph node colors — the tag palette plus the folder pseudo-node
// slate. Held here (not in the page) so the page stays free of strings that
// look like ava-namespaced localStorage keys: the tag name "ava-internal" is
// note data (a memory tag), and the localStorage-policy scan would otherwise
// flag it as an unallowlisted storage key.

const TAG_COLORS: Record<string, string> = {
  "user-profile": "#0284c7",
  "ava-internal": "#475569",
  intelligence: "#7c3aed",
  "org-collab": "#db2777",
  "career-strategy": "#d97706",
  "life-log": "#16a34a",
  "tech-ops": "#0891b2",
  hobby: "#65a30d",
  health: "#dc2626",
  infra: "#6366f1",
  personal: "#f59e0b",
  projects: "#14b8a6",
  agents: "#8b5cf6",
  untagged: "#737373",
};

export function colorForTag(tag: string): string {
  return TAG_COLORS[tag] ?? TAG_COLORS.untagged;
}

// Folder pseudo nodes share one neutral slate so the structure reads as
// backdrop and note tag colors stay the only color signal.
export const FOLDER_COLOR = "#64748b";
