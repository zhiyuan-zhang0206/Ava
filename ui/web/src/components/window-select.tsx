// WindowSelect — the project's one time-window dropdown.
//
// Extracted from the standard pattern that already existed in several places
// (the sidebar's stats window, the fleet Graph View's window, and the Task
// Graph's time filter) — user ruling 2026-08-30: range pickers must reuse one
// shared component instead of spawning a new variant per surface. Each
// surface passes its own option set and className; the control is identical.

export interface WindowOption {
  readonly value: string;
  readonly label: string;
}

export function WindowSelect({
  value,
  options,
  onChange,
  ariaLabel,
  className,
}: {
  value: string;
  options: readonly WindowOption[];
  onChange: (value: string) => void;
  ariaLabel: string;
  className?: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={ariaLabel}
      className={className}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
