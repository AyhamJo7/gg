const PHASES = [
  { key: "ANALYZING", label: "Analyze" },
  { key: "PLANNING", label: "Planning" },
  { key: "IMPLEMENTING", label: "Implementation" },
  { key: "TESTING", label: "Testing" },
  { key: "REVIEWING", label: "Review" },
  { key: "FINAL_VALIDATION", label: "Validation" },
];

export function WorkflowTimeline({ currentPhase, status }: { currentPhase: string | null; status: string }) {
  const phase = status === "REPAIRING" ? "REVIEWING" : currentPhase ?? status;
  const activeIdx = PHASES.findIndex((p) => p.key === phase);
  const doneAll = status === "COMPLETED";
  return (
    <div className="timeline" data-testid="timeline">
      {PHASES.map((p, i) => {
        const done = doneAll || (activeIdx >= 0 && i < activeIdx);
        const active = !doneAll && i === activeIdx;
        return (
          <div key={p.key} className={`step ${active ? "active" : ""}`}>
            {i > 0 && <div className={`connector ${done ? "done" : ""}`} />}
            <div className={`node ${done ? "done" : ""} ${active ? "active" : ""}`}>
              {done ? "✓" : active ? "●" : "○"}
            </div>
            <span className="label">{p.label}</span>
          </div>
        );
      })}
    </div>
  );
}
