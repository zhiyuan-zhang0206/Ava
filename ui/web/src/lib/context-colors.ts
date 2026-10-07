// Fixed, distinguishable colors per context category (a closed set). Mid-tones
// that read on both the light and dark popover surface. The context breakdown and the run
// timeline's blocks draw from this one palette, so a kind has the same color in both.
export const CATEGORY_COLOR: Record<string, string> = {
  system_prompt: "#6366f1",
  compact_summary: "#a855f7",
  cluster_memory: "#0ea5e9",
  agent_memory: "#14b8a6",
  context_note: "#64748b",
  system_notes: "#64748b",
  user_input: "#22c55e",
  agent_messages: "#84cc16",
  automation: "#f97316",
  reasoning: "#ec4899",
  output: "#3b82f6",
  tool_call: "#f59e0b",
  tool_response: "#ef4444",
};

export function categoryColor(kind: string): string {
  return CATEGORY_COLOR[kind] ?? "#94a3b8";
}
