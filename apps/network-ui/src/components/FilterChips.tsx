import { statusTone } from "./StatusBadge";

export interface ChipOption {
  value: string;
  label: string;
  count?: number;
  // Status word used to color the dot (e.g. "healthy"); omit for "All".
  status?: string;
}

interface FilterChipsProps {
  label: string;
  options: ChipOption[];
  value: string;
  onChange: (value: string) => void;
}

/** Single-select segmented filter; each chip shows text + count, not color alone. */
export function FilterChips({ label, options, value, onChange }: FilterChipsProps) {
  return (
    <div className="chip-group" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className="chip"
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.status && (
            <span className={`tone-dot tone-${statusTone(option.status)}`} aria-hidden="true" />
          )}
          {option.label}
          {option.count !== undefined && <span className="chip-count">{option.count}</span>}
        </button>
      ))}
    </div>
  );
}
