function fgColor(value: number): string {
  if (value <= 25) return "var(--red)";
  if (value <= 40) return "var(--orange)";
  if (value <= 60) return "var(--yellow)";
  if (value <= 75) return "var(--green)";
  return "var(--green)";
}

function fgLabel(value: number): string {
  if (value <= 25) return "Extreme Fear";
  if (value <= 40) return "Fear";
  if (value <= 60) return "Neutral";
  if (value <= 75) return "Greed";
  return "Extreme Greed";
}

export function FearGreedGauge({ value }: { value: number }) {
  const color = fgColor(value);
  return (
    <div className="card" style={{ textAlign: "center" }}>
      <div style={{ fontSize: "0.8rem", color: "var(--text-muted)", marginBottom: "0.5rem" }}>
        FEAR & GREED INDEX
      </div>
      <div style={{ fontSize: "2.5rem", fontWeight: 700, color }}>{value}</div>
      <div style={{ fontSize: "0.85rem", color, marginBottom: "0.5rem" }}>{fgLabel(value)}</div>
      <div className="gauge-bar">
        <div className="gauge-fill" style={{ width: `${value}%`, background: color }} />
      </div>
    </div>
  );
}
