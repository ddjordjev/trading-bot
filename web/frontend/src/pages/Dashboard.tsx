import type { FullSnapshot } from "../hooks/useWebSocket";
import { post } from "../api/client";
import { TierBadge } from "../components/TierBadge";
import { PositionRow } from "../components/PositionRow";
import { LogViewer } from "../components/LogViewer";
import { useState } from "react";

export function Dashboard({ data }: { data: FullSnapshot | null }) {
  const [actionMsg, setActionMsg] = useState("");
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
          <div className="label">Balance</div>
          <div className="value">${s.balance.toLocaleString(undefined, { minimumFractionDigits: 2 })}</div>
        </div>
        <div className="stat-card">
          <div className="label">Daily PnL</div>
          <div className={`value ${pnlClass}`}>
            {s.daily_pnl_pct >= 0 ? "+" : ""}{s.daily_pnl_pct.toFixed(2)}%
          </div>
        </div>
        <div className="stat-card">
          <div className="label">Tier</div>
          <div className="value"><TierBadge tier={s.tier} /></div>
        </div>
        <div className="stat-card">
          <div className="label">Target Progress</div>
          <div className="value">{s.tier_progress_pct.toFixed(0)}%</div>
        </div>
        <div className="stat-card">
          <div className="label">Total Growth</div>
          <div className={`value ${s.total_growth_pct >= 0 ? "pnl-positive" : "pnl-negative"}`}>
            {s.total_growth_pct >= 0 ? "+" : ""}{s.total_growth_pct.toFixed(1)}%
          </div>
        </div>
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
        <div className="stat-card">
          <div className="label">Strategies</div>
          <div className="value">{s.strategies_count + s.dynamic_strategies_count}</div>
        </div>
      </div>

      <div className="controls">
        <button className="btn-success" onClick={() => doAction("Start", () => post("/api/bot/start"))}>
          Start Bot
        </button>
        <button className="btn-danger" onClick={() => doAction("Stop", () => post("/api/bot/stop"))}>
          Stop Bot
        </button>
        <button className="btn-warning" onClick={() => doAction("Halt", () => post("/api/stop-trading"))}>
          Halt Trading
        </button>
        <button className="btn-primary" onClick={() => doAction("Resume", () => post("/api/resume-trading"))}>
          Resume Trading
        </button>
        <button className="btn-danger" onClick={() => {
          if (confirm("Close ALL positions?")) doAction("Close All", () => post("/api/close-all"));
        }}>
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
                <th>Symbol</th>
                <th>Entry</th>
                <th>Current</th>
                <th>PnL</th>
                <th>Stop Loss</th>
                <th>Strategy</th>
                <th>Age</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.positions.map((p) => (
                <PositionRow key={p.symbol} position={p} onAction={() => {}} />
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
                <tr key={ws.symbol}>
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

      <h3 style={{ color: "var(--heading)", margin: "1.5rem 0 0.5rem" }}>
        Live Logs
      </h3>
      <LogViewer logs={data.logs ?? []} />
    </div>
  );
}
