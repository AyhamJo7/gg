export function UsageValue({ value, label }: { value: number | null | undefined; label: string }) {
  if (value === null || value === undefined) {
    return (
      <span className="muted" title={`${label}: not captured`}>
        Unknown
      </span>
    );
  }
  return <span className="mono">{value.toLocaleString()}</span>;
}

export function EstimateValue({ tokens }: { tokens: number | null | undefined }) {
  if (tokens === null || tokens === undefined) {
    return <span className="muted">not captured</span>;
  }
  const k = tokens / 1000;
  const text = k >= 1 ? `~${k.toFixed(1)}k estimated` : `~${tokens} estimated`;
  return (
    <span className="mono" title="Local estimate (chars/4), not tokenizer precision">
      {text}
    </span>
  );
}
