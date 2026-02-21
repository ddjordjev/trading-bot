import { useState } from "react";
import type { PositionInfo } from "../hooks/useWebSocket";
import { postBody } from "../api/client";

interface Props {
  position: PositionInfo & { bot_id?: string; exchange_name?: string };
  onAction: () => void;
  showBot?: boolean;
}

const BOT_COLORS: Record<string, string> = {
  momentum: "#58a6ff",
  meanrev: "#d29922",
  swing: "#a371f7",
};

export function PositionRow({ position: p, onAction, showBot = false }: Props) {
  const [loading, setLoading] = useState("");

  const act = async (action: string, fn: () => Promise<unknown>) => {
    setLoading(action);
    try {
      await fn();
      onAction();
    } catch (e) {
      console.error(e);
    }
    setLoading("");
  };

  const pnlClass = p.pnl_pct >= 0 ? "pnl-positive" : "pnl-negative";

  const botId = (p as any).bot_id || "";
  const exchangeName = (p as any).exchange_name || "";

  return (
    <tr>
      {showBot && (
        <td>
          <span style={{
            fontSize: "0.75rem",
            fontWeight: 600,
            color: BOT_COLORS[botId] || "var(--text-muted)",
            textTransform: "uppercase",
          }}>
            {botId || "—"}
          </span>
        </td>
      )}
      {showBot && (
        <td>
          <span style={{
            fontSize: "0.75rem",
            fontWeight: 500,
            color: "var(--text-muted)",
            textTransform: "uppercase",
          }}>
            {exchangeName || "—"}
          </span>
        </td>
      )}
      <td>
        {p.trade_url ? (
          <a
            href={p.trade_url}
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: "var(--accent)", textDecoration: "none", fontWeight: 700 }}
          >
            {p.symbol} ↗
          </a>
        ) : (
          <strong>{p.symbol}</strong>
        )}
        <br />
        <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
          {p.side.toUpperCase()} · {p.leverage}x · {p.market_type}
        </span>
      </td>
      <td>{p.entry_price.toFixed(p.entry_price < 1 ? 6 : 2)}</td>
      <td>{p.current_price.toFixed(p.current_price < 1 ? 6 : 2)}</td>
      <td>${Math.round(p.notional_value).toLocaleString()}</td>
      <td>${Math.round(p.notional_value / Math.max(p.leverage, 1)).toLocaleString()}</td>
      <td className={pnlClass} style={{ fontWeight: 600 }}>
        {p.pnl_pct >= 0 ? "+" : ""}{p.pnl_pct.toFixed(2)}%
        <br />
        <span style={{ fontSize: "0.8rem" }}>
          ${p.pnl_usd >= 0 ? "+" : ""}{p.pnl_usd.toFixed(2)}
        </span>
      </td>
      <td>
        {p.stop_loss ? p.stop_loss.toFixed(p.stop_loss < 1 ? 6 : 2) : "—"}
        {(() => {
          let label: "PR" | "BE" | "LO" = "LO";
          if (p.stop_loss && p.entry_price > 0) {
            const e = p.entry_price;
            const tick =
              e >= 10000 ? 10
              : e >= 100 ? 1
              : e >= 10 ? 0.1
              : e >= 1 ? 0.001
              : e * 0.1;
            const isLong = p.side.toLowerCase() === "long";
            const diff = isLong ? p.stop_loss - e : e - p.stop_loss;
            label = diff > tick ? "PR" : diff >= 0 ? "BE" : "LO";
          }
          const color = label === "PR" ? "var(--green)" : label === "LO" ? "var(--red)" : "var(--yellow)";
          return (
            <span className="badge" style={{ background: color + "22", color, marginLeft: 4, fontSize: "0.65rem" }}>
              {label}{p.breakeven_locked ? " ✓" : ""}
            </span>
          );
        })()}
      </td>
      <td>
        <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
          {p.strategy}
        </span>
        <br />
        <span style={{ fontSize: "0.7rem" }}>
          {p.scale_mode && `${p.scale_mode.toUpperCase()} · ${p.dca_count} adds`}
        </span>
      </td>
      <td style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
        {p.age_minutes < 60
          ? `${p.age_minutes.toFixed(0)}m`
          : `${(p.age_minutes / 60).toFixed(1)}h`}
      </td>
      <td>
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          <button
            className="btn-danger"
            title="Close this position at market."
            disabled={loading === "close"}
            onClick={() => act("close", () => postBody("/api/position/close", { symbol: p.symbol }))}
          >
            Close
          </button>
          <button
            className="btn-warning"
            title="Take profit: close 25% of this position at market."
            style={{ fontSize: "0.75rem", padding: "0.25rem 0.5rem" }}
            disabled={loading === "tp25"}
            onClick={() =>
              act("tp25", () => postBody("/api/position/take-profit", { symbol: p.symbol, pct: 25 }))
            }
          >
            25%
          </button>
          <button
            className="btn-primary"
            title="Tighten trailing stop (move stop loss closer to current price)."
            style={{ fontSize: "0.75rem", padding: "0.25rem 0.5rem" }}
            disabled={loading === "stop"}
            onClick={() =>
              act("stop", () => postBody("/api/position/tighten-stop", { symbol: p.symbol, pct: 2 }))
            }
          >
            Tighten
          </button>
        </div>
      </td>
    </tr>
  );
}
