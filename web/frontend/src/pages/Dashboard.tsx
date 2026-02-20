import type { FullSnapshot } from "../hooks/useWebSocket";
import { post } from "../api/client";
import { PositionRow } from "../components/PositionRow";
import { LogViewer } from "../components/LogViewer";
import { useState } from "react";

export function Dashboard({ data, showBotColumn = false }: { data: FullSnapshot | null; showBotColumn?: boolean }) {
  const [actionMsg, setActionMsg] = useState("");
  const [logsExpanded, setLogsExpanded] = useState(true);

  if (!data) return <div className="empty-state">Connecting...</div>;

  const s = data.status;
  const pnlClass = s.daily_pnl_pct >= 0 ? "pnl-positive" : "pnl-negative";

  const doAction = async (label: string, fn: () => Promise<unknown>) => {
    try {
      await fn();
      setActionMsg(`${label} -- OK`);
    } catch (e: any) {
      setActionMsg(`${label} failed: ${e.message}`);
    }
    setTimeout(() => setActionMsg(""), 3000);
  };

  const fmtUptime = (sec: number) => {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  };

  return (
    <div>
      <div className="stat-grid">
        <div className="stat-card">
          <div className="label">Balance / Available</div>
          <div className="value">${Math.round(s.balance).toLocaleString()}</div>
          <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: 2 }}>
            ${Math.round(s.available_margin).toLocaleString()} available
          </div>
        </div>
        <div className="stat-card">
          <div className="label">Daily PnL</div>
          <div className={`value ${pnlClass}`}>
            {s.daily_pnl >= 0 ? "+" : ""}${s.daily_pnl.toFixed(2)}
          </div>
          <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: 2 }}>
            {s.daily_pnl_pct >= 0 ? "+" : ""}{s.daily_pnl_pct.toFixed(2)}%
          </div>
        </div>
        <div className="stat-card">
          <div className="label">Total Growth</div>
          <div className={`value ${s.total_growth_pct >= 0 ? "pnl-positive" : "pnl-negative"}`}>
            {s.total_growth_usd >= 0 ? "+" : ""}${s.total_growth_usd.toFixed(2)}
          </div>
          <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: 2 }}>
            {s.total_growth_pct >= 0 ? "+" : ""}{s.total_growth_pct.toFixed(1)}%
          </div>
        </div>
        {s.profit_buffer_pct > 0 && (
          <div className="stat-card">
            <div className="label">Profit Buffer</div>
            <div className="value" style={{ color: "var(--green)" }}>
              {s.profit_buffer_pct.toFixed(1)}%
            </div>
            <button
              style={{
                marginTop: 6, padding: "3px 10px", fontSize: "0.7rem",
                background: "var(--surface)", border: "1px solid var(--border)",
                color: "var(--text)", borderRadius: 4, cursor: "pointer",
              }}
              title="Lock in your profits: resets the house-money buffer to 0 and restores the base daily loss limit."
              onClick={() => doAction("Lock Profits", () => post("/api/reset-profit-buffer"))}
            >
              Lock Profits
            </button>
          </div>
        )}
        <div className="stat-card">
          <div className="label">Bot Status</div>
          <div className="value" style={{ color: s.running ? "var(--green)" : "var(--red)" }}>
            {s.running ? "Running" : "Stopped"}
            {s.manual_stop_active && <span style={{ color: "var(--yellow)", fontSize: "0.7rem" }}> (HALTED)</span>}
          </div>
        </div>
        {s.exchange_url && (
          <div className="stat-card">
            <div className="label">Exchange</div>
            <div className="value">
              <a
                href={s.exchange_url}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: "var(--accent)", textDecoration: "none", fontSize: "0.9rem" }}
              >
                {s.exchange_name} ↗
              </a>
              <div style={{ fontSize: "0.65rem", color: "var(--text-muted)", marginTop: 2 }}>
                {s.trading_mode.startsWith("paper") ? s.trading_mode.toUpperCase().replace("_", " ") : "LIVE"}
              </div>
            </div>
          </div>
        )}
        <div className="stat-card">
          <div className="label">Uptime</div>
          <div className="value">{fmtUptime(s.uptime_seconds)}</div>
        </div>
      </div>

      <div className="controls">
        <button
          className="btn-success"
          title="Start the trading bot tick loop; it will begin processing the trade queue and managing positions."
          onClick={() => doAction("Start", () => post("/api/bot/start"))}
        >
          Start Bot
        </button>
        <button
          className="btn-danger"
          title="Stop the bot process; no new trades and no position management until started again."
          onClick={() => doAction("Stop", () => post("/api/bot/stop"))}
        >
          Stop Bot
        </button>
        <button
          className="btn-warning"
          title="Temporarily halt taking new trades; bot keeps running and managing existing positions."
          onClick={() => doAction("Halt", () => post("/api/stop-trading"))}
        >
          Halt Trading
        </button>
        <button
          className="btn-primary"
          title="Resume taking new trades after a halt."
          onClick={() => doAction("Resume", () => post("/api/resume-trading"))}
        >
          Resume Trading
        </button>
        <button
          className="btn-danger"
          title="Close all open positions at market. Use with caution."
          onClick={() => {
            if (confirm("Close ALL positions?")) doAction("Close All", () => post("/api/close-all"));
          }}
        >
          Close All Positions
        </button>
        {actionMsg && (
          <span style={{ alignSelf: "center", fontSize: "0.85rem", color: "var(--accent)" }}>
            {actionMsg}
          </span>
        )}
      </div>

      <h3 style={{ color: "var(--heading)", marginBottom: "0.5rem" }}>
        Open Positions ({data.positions.length})
      </h3>
      {data.positions.length === 0 ? (
        <div className="empty-state">No open positions</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                {showBotColumn && <th>Bot</th>}
                {showBotColumn && <th>Exchange</th>}
                <th>Symbol</th>
                <th>Entry</th>
                <th>Current</th>
                <th>Size</th>
                <th>Margin</th>
                <th>PnL</th>
                <th>Stop Loss</th>
                <th>Strategy</th>
                <th>Age</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.positions.map((p) => (
                <PositionRow key={`${(p as any).bot_id || ""}:${p.symbol}`} position={p} onAction={() => {}} showBot={showBotColumn} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.wick_scalps.length > 0 && (
        <>
          <h3 style={{ color: "var(--heading)", margin: "1.5rem 0 0.5rem" }}>
            Active Wick Scalps ({data.wick_scalps.length})
          </h3>
          <table>
            <thead>
              <tr>
                {showBotColumn && <th>Bot</th>}
                {showBotColumn && <th>Exchange</th>}
                <th>Symbol</th>
                <th>Side</th>
                <th>Entry</th>
                <th>Amount</th>
                <th>Age</th>
                <th>Max Hold</th>
              </tr>
            </thead>
            <tbody>
              {data.wick_scalps.map((ws) => (
                <tr key={`${(ws as any).bot_id || ""}:${ws.symbol}`}>
                  {showBotColumn && <td style={{ fontSize: "0.75rem", fontWeight: 600, textTransform: "uppercase", color: "var(--text-muted)" }}>{(ws as any).bot_id || "—"}</td>}
                  {showBotColumn && <td style={{ fontSize: "0.75rem", fontWeight: 500, textTransform: "uppercase", color: "var(--text-muted)" }}>{(ws as any).exchange_name || "—"}</td>}
                  <td>{ws.symbol}</td>
                  <td>{ws.scalp_side.toUpperCase()}</td>
                  <td>{ws.entry_price.toFixed(ws.entry_price < 1 ? 6 : 2)}</td>
                  <td>{ws.amount.toFixed(6)}</td>
                  <td>{ws.age_minutes.toFixed(0)}m</td>
                  <td>{ws.max_hold_minutes}m</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <div
        style={{ display: "flex", alignItems: "center", gap: "0.5rem", margin: "1.5rem 0 0.5rem", cursor: "pointer" }}
        onClick={() => setLogsExpanded(!logsExpanded)}
      >
        <span style={{ fontSize: "1.4rem", lineHeight: 1, color: "var(--text-muted)", transition: "transform 0.2s", transform: logsExpanded ? "rotate(90deg)" : "rotate(0deg)" }}>
          ▶
        </span>
        <h3 style={{ color: "var(--heading)", margin: 0 }}>
          Live Logs
        </h3>
      </div>
      {logsExpanded && <LogViewer logs={data.logs ?? []} />}
    </div>
  );
}
