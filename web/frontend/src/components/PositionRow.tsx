import { useState } from "react";
import type { PositionInfo } from "../hooks/useWebSocket";
import { post, postQuery } from "../api/client";

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

  const sym = encodeURIComponent(p.symbol);
  const pnlClass = p.pnl_pct >= 0 ? "pnl-positive" : "pnl-negative";

  return (
    <tr>
      <td>
        <strong>{p.symbol}</strong>
        <br />
        <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
          {p.side.toUpperCase()} · {p.leverage}x · {p.market_type}
        </span>
      </td>
      <td>{p.entry_price.toFixed(p.entry_price < 1 ? 6 : 2)}</td>
      <td>{p.current_price.toFixed(p.current_price < 1 ? 6 : 2)}</td>
      <td className={pnlClass} style={{ fontWeight: 600 }}>
        {p.pnl_pct >= 0 ? "+" : ""}{p.pnl_pct.toFixed(2)}%
        <br />
        <span style={{ fontSize: "0.8rem" }}>
          ${p.pnl_usd >= 0 ? "+" : ""}{p.pnl_usd.toFixed(2)}
        </span>
      </td>
      <td>
        {p.stop_loss ? p.stop_loss.toFixed(p.stop_loss < 1 ? 6 : 2) : "—"}
        {p.breakeven_locked && (
          <span className="badge" style={{ background: "var(--green)22", color: "var(--green)", marginLeft: 4, fontSize: "0.65rem" }}>
            BE
          </span>
        )}
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
            disabled={loading === "close"}
            onClick={() => act("close", () => post(`/api/position/${sym}/close`))}
          >
            Close
          </button>
          {[25, 50, 75].map((pct) => (
            <button
              key={pct}
              className="btn-warning"
              style={{ fontSize: "0.75rem", padding: "0.25rem 0.5rem" }}
              disabled={loading === `tp${pct}`}
              onClick={() => act(`tp${pct}`, () => postQuery(`/api/position/${sym}/take-profit`, { pct }))}
            >
              {pct}%
            </button>
          ))}
          <button
            className="btn-primary"
            style={{ fontSize: "0.75rem", padding: "0.25rem 0.5rem" }}
            disabled={loading === "stop"}
            onClick={() => act("stop", () => postQuery(`/api/position/${sym}/tighten-stop`, { pct: 2 }))}
          >
            Tighten
          </button>
        </div>
      </td>
    </tr>
  );
}
