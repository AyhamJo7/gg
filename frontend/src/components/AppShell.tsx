import { useMemo } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useTheme } from "../lib/theme";
import type { Command } from "../lib/commands";
import { ActivityButton, ActivityPanel, ActivityProvider } from "./ActivityCenter";
import { CommandPalette } from "./CommandPalette";
import { Icon, type IconName } from "./Icon";

const NAV: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "Overview", icon: "overview" },
  { to: "/missions", label: "Missions", icon: "missions" },
  { to: "/new", label: "New Mission", icon: "plus" },
  { to: "/lifecycle", label: "Products", icon: "products" },
  { to: "/projects", label: "Repositories", icon: "repos" },
  { to: "/providers", label: "Providers", icon: "providers" },
  { to: "/priority", label: "Priority Matrix", icon: "priority" },
  { to: "/analytics", label: "Analytics", icon: "analytics" },
];

const IS_MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

export function AppShell() {
  const [theme, toggleTheme] = useTheme();
  const commands = useMemo<Command[]>(() => [
    ...NAV.map(n => ({ id: `nav-${n.to}`, group: "Go to", label: n.label, href: n.to })),
    { id: "act-product", group: "Create", label: "Build a product", href: "/lifecycle?create=1" },
    { id: "act-mission", group: "Create", label: "Run a mission in a repository", href: "/new" },
    { id: "act-theme", group: "Preferences", label: theme === "dark" ? "Switch to light mode" : "Switch to dark mode", action: toggleTheme },
  ], [theme, toggleTheme]);
  return (
    <ActivityProvider>
      <div className="app-shell">
        <a className="skip-link" href="#main-content" onClick={(event) => { event.preventDefault(); document.getElementById("main-content")?.focus(); }}>Skip to content</a>
        <aside className="sidebar">
          <div className="brand"><span className="brand-mark" aria-hidden>GG</span> Orchestrator</div>
          <button className="palette-trigger" onClick={() => window.dispatchEvent(new Event("gg:open-palette"))}>
            <Icon name="search" /> <span>Search</span> <kbd>{IS_MAC ? "⌘" : "Ctrl"} K</kbd>
          </button>
          <nav aria-label="Main navigation">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.to === "/"}>
                <Icon name={n.icon} /> {n.label}
              </NavLink>
            ))}
          </nav>
          <div className="sidebar-footer">
            <ActivityButton />
            <button onClick={toggleTheme} className="theme-toggle">
              <Icon name={theme === "dark" ? "sun" : "moon"} /> {theme === "dark" ? "Light mode" : "Dark mode"}
            </button>
          </div>
        </aside>
        <main className="main" id="main-content" tabIndex={-1}>
          <Outlet />
        </main>
        <ActivityPanel />
        <CommandPalette staticCommands={commands} />
      </div>
    </ActivityProvider>
  );
}
