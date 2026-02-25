import { useState } from "react";
import type { PositionInfo } from "../hooks/useWebSocket";
import { postBody } from "../api/client";

interface Props {
  position: PositionInfo & { bot_id?: string; exchange_name?: string };
  onAction: () => void;
  showBot?: boolean;
  bulkAction?: string;
}

const BOT_COLORS: Record<string, string> = {
  momentum: "#58a6ff",
  meanrev: "#d29922",
  swing: "#a371f7",
};

export function PositionRow({ position: p, onAction, showBot = false, bulkAction = "" }: Props) {
  const [loading, setLoading] = useState("");
  const [feedback, setFeedback] = useState<{ ok: boolean; msg: string } | null>(null);
  const [closed, setClosed] = useState(false);

  const act = async (action: string, fn: () => Promise<{ success: boolean; message: string }>) => {
    setLoading(action);
    setFeedback(null);
    try {
      const res = await fn();
      setFeedback({ ok: res.success, msg: res.message });
      if (res.success) {
        if (action === "close") setClosed(true);
        onAction();
      }
    } catch (e: any) {
      setFeedback({ ok: false, msg: e?.message || "Request failed" });
    }
    setLoading("");
    if (!closed) setTimeout(() => setFeedback(null), 5000);
  };

  const pnlClass = p.pnl_pct >= 0 ? "pnl-positive" : "pnl-negative";

  const botId = (p as any).bot_id || "";
  const exchangeName = (p as any).exchange_name || "";
  const stopSource = (p.stop_source || "").toLowerCase();
  const isCexActive = stopSource === "exchange";
  const isBotActive = stopSource === "bot";
  const formatStop = (value: number | null | undefined) => {
    if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "—";
    return value.toFixed(value < 1 ? 6 : 2);
  };
  const hasStop = (value: number | null | undefined) => typeof value === "number" && Number.isFinite(value) && value > 0;
  const cexSl = hasStop(p.exchange_stop_loss) ? p.exchange_stop_loss : null;
  const botSl = hasStop(p.bot_stop_loss) ? p.bot_stop_loss : null;

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
        <span
          style={{
            fontSize: "0.75rem",
            color:
              p.side.toLowerCase() === "buy" || p.side.toLowerCase() === "long"
                ? "var(--green)"
                : p.side.toLowerCase() === "sell" || p.side.toLowerCase() === "short"
                  ? "var(--red)"
                  : "var(--text-muted)",
          }}
        >
          {p.side === "buy" ? "LONG" : p.side === "sell" ? "SHORT" : p.side.toUpperCase()} · {p.leverage}x · {p.market_type}
        </span>
      </td>
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
      <td>{p.entry_price.toFixed(p.entry_price < 1 ? 6 : 2)}</td>
      <td>{p.current_price.toFixed(p.current_price < 1 ? 6 : 2)}</td>
      <td>${Math.round(p.notional_value).toLocaleString()}</td>
      <td>${Math.round(p.notional_value / Math.max(p.leverage, 1)).toLocaleString()}</td>
      <td className={pnlClass} style={{ fontWeight: 600 }}>
        <span style={{ fontSize: "0.8rem" }}>
          ${p.pnl_usd >= 0 ? "+" : ""}{p.pnl_usd.toFixed(2)}
        </span>
        <br />
        {p.pnl_pct >= 0 ? "+" : ""}{p.pnl_pct.toFixed(2)}%
      </td>
      <td title="Raw stop values by source (no effective SL fallback).">
        <div>
          <span style={{ color: "var(--text-muted)", fontSize: "0.65rem" }}>CEX</span>{" "}
          {formatStop(cexSl)}
          {cexSl && (
            <span
              className="badge"
              style={{
                marginLeft: 4,
                fontSize: "0.65rem",
                background: (isCexActive ? "var(--green)" : "var(--text-muted)") + "22",
                color: isCexActive ? "var(--green)" : "var(--text-muted)",
              }}
            >
              {isCexActive ? "ACTIVE" : "STANDBY"}
            </span>
          )}
          {cexSl && isCexActive && p.close_pending && (
            <span
              className="badge"
              style={{ background: "var(--red)22", color: "var(--red)", marginLeft: 4, fontSize: "0.65rem" }}
            >
              BREACHED
            </span>
          )}
        </div>
        <div>
          <span style={{ color: "var(--text-muted)", fontSize: "0.65rem" }}>BOT</span>{" "}
          {formatStop(botSl)}
          {botSl && (
            <span
              className="badge"
              style={{
                marginLeft: 4,
                fontSize: "0.65rem",
                background: (isBotActive ? "var(--green)" : "var(--text-muted)") + "22",
                color: isBotActive ? "var(--green)" : "var(--text-muted)",
              }}
            >
              {isBotActive ? "ACTIVE" : "STANDBY"}
            </span>
          )}
          {botSl && isBotActive && p.close_pending && (
            <span
              className="badge"
              style={{ background: "var(--red)22", color: "var(--red)", marginLeft: 4, fontSize: "0.65rem" }}
            >
              BREACHED
            </span>
          )}
        </div>
      </td>
      <td style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
        {p.age_minutes < 60
          ? `${p.age_minutes.toFixed(0)}m`
          : `${(p.age_minutes / 60).toFixed(1)}h`}
      </td>
      <td>
        {closed ? (
          <div style={{ fontSize: "0.75rem", fontWeight: 600, color: "var(--green)" }}>
            Closed
          </div>
        ) : bulkAction ? (
          <div style={{ fontSize: "0.75rem", fontWeight: 600, color: "var(--yellow)" }}>
            {bulkAction}
          </div>
        ) : (
          <>
            <div style={{ display: "flex", gap: 4, flexWrap: "wrap", alignItems: "center" }}>
              <button
                className="btn-danger"
                title="Close this position at market."
                disabled={!!loading}
                onClick={() => act("close", () => postBody("/api/position/close", { symbol: p.symbol, bot_id: botId }))}
              >
                {loading === "close" ? "..." : "Close"}
              </button>
              <button
                className="btn-warning"
                title="Take profit: close 25% of this position at market."
                style={{ fontSize: "0.75rem", padding: "0.25rem 0.5rem" }}
                disabled={!!loading}
                onClick={() =>
                  act("tp25", () => postBody("/api/position/take-profit", { symbol: p.symbol, pct: 25, bot_id: botId }))
                }
              >
                {loading === "tp25" ? "..." : "25%"}
              </button>
              <button
                className="btn-primary"
                title="Tighten trailing stop (move stop loss closer to current price)."
                style={{ fontSize: "0.75rem", padding: "0.25rem 0.5rem" }}
                disabled={!!loading}
                onClick={() =>
                  act("stop", () => postBody("/api/position/tighten-stop", { symbol: p.symbol, pct: 2, bot_id: botId }))
                }
              >
                {loading === "stop" ? "..." : "Tighten"}
              </button>
            </div>
            {feedback && (
              <div style={{
                marginTop: 4,
                fontSize: "0.7rem",
                fontWeight: 600,
                color: feedback.ok ? "var(--green)" : "var(--red)",
              }}>
                {feedback.msg}
              </div>
            )}
          </>
        )}
      </td>
    </tr>
  );
}
