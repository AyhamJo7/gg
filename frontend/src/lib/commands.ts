export interface Command {
  id: string;
  group: string;
  label: string;
  hint?: string;
  href?: string;
  action?: () => void;
}

const EMPTY_QUERY_SCORE = 1;
const PREFIX_SCORE = 100;
const SUBSTRING_SCORE = 80;
const SUBSTRING_OFFSET_PENALTY_CAP = 40;
const SUBSEQUENCE_SCORE = 40;
const MIN_MATCH_SCORE = 1;

/** Case-insensitive subsequence match; scores contiguous and prefix hits
 * higher so "rech" ranks "RechnungsRadar" above "Rescheduled check". */
export function commandScore(label: string, query: string): number {
  const q = query.trim().toLowerCase();
  if (!q) return EMPTY_QUERY_SCORE;
  const l = label.toLowerCase();
  const index = l.indexOf(q);
  if (index === 0) return PREFIX_SCORE;
  if (index > 0) return SUBSTRING_SCORE - Math.min(index, SUBSTRING_OFFSET_PENALTY_CAP);
  let li = 0;
  let gaps = 0;
  for (const ch of q) {
    const found = l.indexOf(ch, li);
    if (found < 0) return 0;
    gaps += found - li;
    li = found + 1;
  }
  return Math.max(MIN_MATCH_SCORE, SUBSEQUENCE_SCORE - gaps);
}

export function commandMatches(commands: Command[], query: string): Command[] {
  return commands
    .map((cmd, order) => ({ cmd, order, score: Math.max(commandScore(cmd.label, query), commandScore(`${cmd.group} ${cmd.label}`, query) - 1) }))
    .filter(r => r.score > 0)
    .sort((a, b) => b.score - a.score || a.order - b.order)
    .map(r => r.cmd);
}
