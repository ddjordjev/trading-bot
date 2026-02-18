const TIER_COLORS: Record<string, string> = {
  losing: "var(--red)",
  building: "var(--accent)",
  strong: "var(--green)",
  excellent: "var(--purple)",
  monster: "var(--orange)",
  legendary: "var(--gold)",
};

export function TierBadge({ tier }: { tier: string }) {
  const color = TIER_COLORS[tier] || "var(--text-muted)";
  return (
    <span
      className="badge"
      style={{
        background: `${color}22`,
        color,
        border: `1px solid ${color}`,
        fontSize: "0.8rem",
        padding: "0.2rem 0.6rem",
      }}
    >
      {tier.toUpperCase()}
    </span>
  );
}
