const BOT_LABEL_MAP: Record<string, string> = {
  momentum: "Momentum",
  meanrev: "Mean Reversion",
  swing: "Swing",
  scalper: "Scalper",
  indicators: "Indicators",
  aggressive: "Aggressive",
  hedger: "Hedger",
  extreme: "Extreme",
  conservative: "Conservative",
  fullstack: "Fullstack",
};

function titleCaseFromId(botId: string): string {
  if (!botId) return "";
  return botId
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function getBotLabel(botId: string): string {
  const id = String(botId || "").trim().toLowerCase();
  if (!id) return "";
  return BOT_LABEL_MAP[id] || titleCaseFromId(id);
}

