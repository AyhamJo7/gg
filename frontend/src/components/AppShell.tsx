import { NavLink, Outlet } from "react-router-dom";
import { useTheme } from "../lib/theme";

const NAV = [
  { to: "/", label: "Mission Control", icon: "◎" },
  { to: "/new", label: "New Mission", icon: "+" },
  { to: "/lifecycle", label: "Idea → Product", icon: "✦" },
  { to: "/projects", label: "Projects", icon: "▤" },
  { to: "/providers", label: "Providers", icon: "⬡" },
  { to: "/priority", label: "Priority Matrix", icon: "⇅" },
  { to: "/analytics", label: "Analytics", icon: "▲" },
];

export function AppShell() {
  const [theme, toggleTheme] = useTheme();
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">⬢ GG Orchestrator</div>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"}>
              <span aria-hidden>{n.icon}</span> {n.label}
            </NavLink>
          ))}
        </nav>
        <div style={{ marginTop: "auto", padding: 12 }}>
          <button onClick={toggleTheme} style={{ width: "100%" }}>
            {theme === "dark" ? "☀ Light mode" : "☾ Dark mode"}
          </button>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
