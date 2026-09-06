import type { CSSProperties } from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import "./theme-switch.css";

export type Theme = "system" | "light" | "dark";

const options = [
  { value: "system", label: "跟随系统", icon: Monitor },
  { value: "light", label: "浅色", icon: Sun },
  { value: "dark", label: "深色", icon: Moon },
] as const;

export default function ThemeSwitch({
  value,
  onChange,
}: {
  value: Theme;
  onChange: (value: Theme) => void;
}) {
  const selected = options.findIndex((option) => option.value === value);
  return (
    <div className="theme-control">
      <div className="theme-caption" aria-hidden="true">
        <span>外观</span>
        <span>{options[selected].label}</span>
      </div>
      <div
        className="theme-switch"
        role="radiogroup"
        aria-label="外观主题"
        style={
          {
            "--theme-index": selected,
            "--theme-count": options.length,
          } as CSSProperties
        }
      >
        <span className="theme-switch-indicator" aria-hidden="true" />
        {options.map(({ value: option, label, icon: Icon }) => (
          <label className="theme-option" key={option} title={label}>
            <input
              type="radio"
              name="appearance"
              value={option}
              aria-label={label}
              checked={value === option}
              onChange={() => onChange(option)}
            />
            <Icon size={17} strokeWidth={1.7} aria-hidden="true" />
          </label>
        ))}
      </div>
    </div>
  );
}
