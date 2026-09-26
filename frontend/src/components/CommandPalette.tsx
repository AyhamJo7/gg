import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { commandMatches, type Command } from "../lib/commands";
import { missionHref, operatorLabel } from "../lib/operator";

const MAX_RESULTS = 12;

export function CommandPalette({ staticCommands }: { staticCommands: Command[] }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [dynamic, setDynamic] = useState<Command[]>([]);
  const [loadError, setLoadError] = useState(false);
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const restoreFocus = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(o => !o);
      }
    };
    const onOpen = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("gg:open-palette", onOpen);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("gg:open-palette", onOpen); };
  }, []);

  useEffect(() => {
    if (!open) return;
    restoreFocus.current = document.activeElement as HTMLElement | null;
    setQuery(""); setActive(0); setLoadError(false);
    inputRef.current?.focus();
    let cancelled = false;
    Promise.all([api.missions.list(), api.lifecycle.list()])
      .then(([missions, products]) => {
        if (cancelled) return;
        setDynamic([
          ...missions.map(m => ({ id: `m-${m.id}`, group: "Missions", label: m.title, hint: operatorLabel(m.status), href: missionHref(m.id) })),
          ...products.map(p => ({ id: `p-${p.id}`, group: "Products", label: p.name, hint: operatorLabel(p.state), href: `/lifecycle/${encodeURIComponent(p.id)}` })),
        ]);
      })
      .catch(() => { if (!cancelled) setLoadError(true); });
    return () => { cancelled = true; restoreFocus.current?.focus?.(); };
  }, [open]);

  const results = useMemo(
    () => commandMatches([...staticCommands, ...dynamic], query).slice(0, MAX_RESULTS),
    [staticCommands, dynamic, query],
  );

  if (!open) return null;
  const run = (cmd: Command | undefined) => {
    if (!cmd) return;
    setOpen(false);
    if (cmd.href) navigate(cmd.href);
    cmd.action?.();
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(a => Math.min(a + 1, results.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(a => Math.max(a - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); run(results[active]); }
    else if (e.key === "Escape") { e.preventDefault(); setOpen(false); }
  };
  return (
    <div className="palette-backdrop" onMouseDown={() => setOpen(false)}>
      <div className="palette" role="dialog" aria-modal="true" aria-label="Command palette" onMouseDown={e => e.stopPropagation()}>
        <input
          ref={inputRef}
          value={query}
          onChange={e => { setQuery(e.target.value); setActive(0); }}
          onKeyDown={onKeyDown}
          placeholder="Jump to a mission, product or page…"
          aria-label="Search commands"
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-results"
          aria-activedescendant={results[active] ? `palette-${results[active].id}` : undefined}
        />
        <ul id="palette-results" role="listbox" className="palette-results">
          {results.map((cmd, i) => (
            <li
              key={cmd.id}
              id={`palette-${cmd.id}`}
              role="option"
              aria-selected={i === active}
              className={i === active ? "active" : ""}
              onMouseEnter={() => setActive(i)}
              onClick={() => run(cmd)}
            >
              <span className="palette-group muted">{cmd.group}</span>
              <span className="palette-label">{cmd.label}</span>
              {cmd.hint && <span className="palette-hint muted">{cmd.hint}</span>}
            </li>
          ))}
          {!results.length && <li className="muted palette-empty">No matches.</li>}
        </ul>
        <footer className="palette-footer muted">
          ↑↓ to move · Enter to open · Esc to close{loadError && " · missions and products could not be loaded"}
        </footer>
      </div>
    </div>
  );
}
