import { missionVerdict, reviewIndependenceText, type VerdictTone } from "../lib/operator";
import type { Mission, ReviewSummary } from "../lib/types";

const TONE_CLASS: Record<VerdictTone, string> = {
  clean: "green",
  caveat: "orange",
  stopped: "purple",
  active: "blue",
  unknown: "",
};

export function MissionVerdictBadge({ mission }: { mission: Pick<Mission, "status" | "trust"> }) {
  const verdict = missionVerdict(mission);
  return (
    <span className="verdict" data-testid="mission-verdict">
      <span className={`badge ${TONE_CLASS[verdict.tone]}`}>
        <span className="dot" />
        {verdict.label}
      </span>
      {verdict.caveats.length > 0 && (
        <span className="verdict-caveats muted">{verdict.caveats.join(" · ")}</span>
      )}
    </span>
  );
}

function shortSha(sha: string | null): string {
  return sha ? sha.slice(0, 8) : "unknown";
}

/** Latest review independence, stated from recorded evidence only. */
export function ReviewTrustCard({ review }: { review: ReviewSummary }) {
  if (review.independent) return null;
  const text = reviewIndependenceText(review);
  return (
    <div className="card attention-card" data-testid="review-trust-warning">
      <div className="row">
        <span className="badge orange"><span className="dot" />{text.badge}</span>
        <strong>{text.title}</strong>
      </div>
      <dl className="facts">
        <dt>Reviewer</dt><dd className="mono">{review.reviewer || "unknown"}</dd>
        <dt>Recorded reason</dt><dd>{text.detail}</dd>
        <dt>Recorded writers</dt>
        <dd className="mono">{review.writer_set.length ? review.writer_set.join(", ") : "not recorded"}</dd>
        <dt>Reviewed range</dt>
        <dd className="mono">{shortSha(review.reviewed_base_sha)} → {shortSha(review.reviewed_head_sha)}</dd>
      </dl>
      <p className="muted">
        Findings from this review are real, but GG cannot certify that an independent provider checked this candidate.
      </p>
    </div>
  );
}
