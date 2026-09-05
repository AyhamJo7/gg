import type { GitState } from "../lib/types";

export function GitPanel({ git }: { git: GitState | null }) {
  if (!git) return <div className="muted">Loading git state…</div>;
  if (!git.is_repo) return <div className="muted">Not a git repository yet</div>;
  const changes = git.modified.length + git.added.length + git.deleted.length + git.untracked.length;
  return (
    <div className="stack">
      <div className="row spread">
        <div className="row">
          <span className="badge">{git.branch || "detached"}</span>
          <span className="mono muted">{(git.head ?? "").slice(0, 8)}</span>
        </div>
        <span className="muted">{changes} change{changes === 1 ? "" : "s"}</span>
      </div>
      {changes > 0 && (
        <div>
          {git.modified.map((f) => <div key={f} className="mono">M {f}</div>)}
          {git.added.map((f) => <div key={f} className="mono" style={{ color: "var(--green)" }}>A {f}</div>)}
          {git.deleted.map((f) => <div key={f} className="mono" style={{ color: "var(--red)" }}>D {f}</div>)}
          {git.untracked.map((f) => <div key={f} className="mono faint">? {f}</div>)}
        </div>
      )}
      {git.diff && (
        <pre className="diff-view" data-testid="diff-view">
          {git.diff.split("\n").map((line, i) => (
            <div key={i} className={line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : ""}>{line}</div>
          ))}
        </pre>
      )}
      <div>
        <h3>Recent commits</h3>
        {git.recent_commits.map((c) => (
          <div key={c} className="mono muted" style={{ padding: "1px 0" }}>{c}</div>
        ))}
      </div>
    </div>
  );
}
