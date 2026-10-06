// Small inline line-icon set (24x24 grid, stroke = currentColor). No icon dependency:
// each icon is a few SVG primitives, decorative only (aria-hidden), so a text label
// always carries the meaning.

const PATHS = {
  grid: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z",
  home: "M3 11 12 4l9 7M5 10v10h5v-6h4v6h5V10",
  server: "M4 5h16v5H4zM4 14h16v5H4zM8 7.5h.01M8 16.5h.01M12 7.5h5M12 16.5h5",
  topology: "M12 4v5M6.5 15 10 11M17.5 15 14 11M10 4h4v5h-4zM3 15h6v5H3zM15 15h6v5h-6z",
  pulse: "M3 12h4l2.5-6 5 12 2.5-6H21",
  wrench: "M14.5 5.5a4 4 0 0 0 4.9 4.9L21 12l-9 9-3-3 9-9-1.6-1.6A4 4 0 0 0 14.5 5.5ZM3 21l4.5-4.5",
  checkCircle: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM8 12.5l2.7 2.7L16 10",
  jobs: "M5 4h14v16H5zM9 9l2 2 4-4M9 15h6",
  archive: "M3 5h18v4H3zM5 9v10h14V9M10 13h4",
  audit: "M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h7",
  terminal: "M4 5h16v14H4zM8 10l3 2-3 2M13 15h3",
  bell: "M6 16V11a6 6 0 1 1 12 0v5l1.5 2h-15zM10 20.5a2 2 0 0 0 4 0",
  refresh: "M20 11a8 8 0 0 0-14.5-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.5 4.5L20 16M20 20v-4h-4",
  play: "M8 5v14l11-7z",
  chevronRight: "M9 6l6 6-6 6",
  arrowRight: "M5 12h14M13 6l6 6-6 6",
  arrowDown: "M12 5v14M6 13l6 6 6-6",
  check: "M5 12.5l4.5 4.5L19 7.5",
  alert: "M12 4 2.5 20h19zM12 10v4.5M12 17.5h.01",
  alertCircle: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 7.5v6M12 16.5h.01",
  help: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5v.7M12 17h.01",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 7v5l3 2",
  fileCheck: "M6 3h9l4 4v14H6zM14 3v5h5M9.5 14.5l2 2 3.5-3.5",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM19.4 13.5l1.6 1.2-2 3.4-1.9-.7a7 7 0 0 1-2 1.2L14.8 21h-4l-.3-2.4a7 7 0 0 1-2-1.2l-1.9.7-2-3.4 1.6-1.2a7 7 0 0 1 0-3L4.6 9.3l2-3.4 1.9.7a7 7 0 0 1 2-1.2L10.8 3h4l.3 2.4a7 7 0 0 1 2 1.2l1.9-.7 2 3.4-1.6 1.2a7 7 0 0 1 0 3Z",
  database: "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3ZM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  box: "M12 3 4 7.5v9L12 21l8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9",
  shield: "M12 3 5 6v5c0 4.5 3 8.3 7 10 4-1.7 7-5.5 7-10V6zM9 12l2 2 4-4",
  layers: "M12 3 3 8l9 5 9-5zM3 13l9 5 9-5M3 17.5 12 22l9-4.5",
  upload: "M12 16V4M7 9l5-5 5 5M4 16v4h16v-4",
  download: "M12 4v12M7 11l5 5 5-5M4 20h16",
  file: "M6 3h9l4 4v14H6zM14 3v5h5",
  x: "M6 6l12 12M18 6 6 18",
  cpu: "M5 5h14v14H5zM9 9h6v6H9zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3",
  memory: "M3 7h18v9H3zM7 10.5h2M11 10.5h2M15 10.5h2M6 16v3M10 16v3M14 16v3M18 16v3",
} as const;

export type IconName = keyof typeof PATHS;

interface IconProps {
  name: IconName;
  size?: number;
  className?: string;
}

export function Icon({ name, size = 16, className }: IconProps) {
  return (
    <svg
      className={`icon${className ? ` ${className}` : ""}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}

/** Square tinted tile holding an icon (KPI cards, service cards, list rows). */
export function IconTile({ name, tone = "info", size = 16 }: { name: IconName; tone?: string; size?: number }) {
  return (
    <span className={`icon-tile icon-tile-${tone}`} aria-hidden="true">
      <Icon name={name} size={size} />
    </span>
  );
}
