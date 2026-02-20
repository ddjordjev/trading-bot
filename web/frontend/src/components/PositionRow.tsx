import { useState } from "react";
import type { PositionInfo } from "../hooks/useWebSocket";
import { postBody } from "../api/client";

interface Props {
  position: PositionInfo;
  onAction: () => void;
}

export function PositionRow({ position: p, onAction }: Props) {
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

  return (
    <tr>
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
        {p.breakeven_locked && p.stop_loss != null && (() => {
          const isLong = p.side.toLowerCase() === "buy" || p.side.toLowerCase() === "long";
          const inProfit = isLong ? p.stop_loss > p.entry_price : p.stop_loss < p.entry_price;
          const inLoss = isLong ? p.stop_loss < p.entry_price : p.stop_loss > p.entry_price;
          const label = inProfit ? "PR" : inLoss ? "LO" : "BE";
          const color = inProfit ? "var(--green)" : inLoss ? "var(--red)" : "var(--yellow)";
          return (
            <span className="badge" style={{ background: color + "22", color, marginLeft: 4, fontSize: "0.65rem" }}>
              {label}
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
          {[25, 50, 75].map((pct) => (
            <button
              key={pct}
              className="btn-warning"
              title={`Take profit: close ${pct}% of this position at market.`}
              style={{ fontSize: "0.75rem", padding: "0.25rem 0.5rem" }}
              disabled={loading === `tp${pct}`}
              onClick={() =>
                act(`tp${pct}`, () => postBody("/api/position/take-profit", { symbol: p.symbol, pct }))
              }
            >
              {pct}%
            </button>
          ))}
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
