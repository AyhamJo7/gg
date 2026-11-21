/** Minimal stroke icon set (24px grid, currentColor) replacing mixed unicode glyphs. */
const PATHS = {
  overview: "M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 12m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0",
  missions: "M4 6h16M4 12h16M4 18h10",
  plus: "M12 5v14M5 12h14",
  products: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12v9M12 12L4 7.5",
  repos: "M6 3v12M18 9a3 3 0 1 0 0-6a3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6a3 3 0 0 0 0 6zM18 9a9 9 0 0 1-9 9",
  providers: "M4 7h16v4H4zM4 13h16v4H4zM8 9h.01M8 15h.01",
  priority: "M7 4v16M3 8l4-4l4 4M17 20V4M13 16l4 4l4-4",
  analytics: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  search: "M11 18a7 7 0 1 0 0-14a7 7 0 0 0 0 14zM21 21l-5-5",
  sun: "M12 17a5 5 0 1 0 0-10a5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden focusable="false">
      <path d={PATHS[name]} />
    </svg>
  );
}
