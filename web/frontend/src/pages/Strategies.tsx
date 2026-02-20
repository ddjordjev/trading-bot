import { useEffect, useState } from "react";
import { get } from "../api/client";

interface Strategy {
  name: string;
  symbol: string;
  market_type: string;
  leverage: number;
  mode: string;
  is_dynamic: boolean;
  open_now: number;
  applied_count: number;
  success_count: number;
  fail_count: number;
  bot_id?: string;
}

export function Strategies({ bots = [] }: { bots?: { bot_id: string; label?: string }[] }) {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    get<Strategy[]>("/api/strategies")
      .then(setStrategies)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const multiBot = strategies.some((s) => s.bot_id);

  if (loading) return <div className="empty-state">Loading...</div>;
  if (strategies.length === 0) return <div className="empty-state">No active strategies</div>;

  const staticS = strategies.filter((s) => !s.is_dynamic);
  const dynamicS = strategies.filter((s) => s.is_dynamic);

  const renderTable = (rows: Strategy[]) => (
    <table style={{ marginBottom: "2rem" }}>
      <thead>
        <tr>
          {multiBot && <th>Bot</th>}
          <th>Strategy</th>
          <th>Symbol</th>
          <th>Market</th>
          <th>Leverage</th>
          <th>Mode</th>
          <th>Open</th>
          <th>Closed</th>
          <th>Wins</th>
          <th>Losses</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((s, i) => (
          <tr key={`${s.bot_id}:${s.name}:${s.symbol}:${i}`}>
            {multiBot && (
              <td style={{ fontSize: "0.75rem", fontWeight: 600, textTransform: "uppercase", color: "var(--text-muted)" }}>
                {s.bot_id || "—"}
              </td>
            )}
            <td><strong>{s.name}</strong></td>
            <td>{s.symbol}</td>
            <td>{s.market_type}</td>
            <td>{s.leverage}x</td>
            <td>
              <span className="badge" style={{
                background: s.mode === "pyramid" ? "rgba(188,140,255,0.15)" : "rgba(88,166,255,0.15)",
                color: s.mode === "pyramid" ? "var(--purple)" : "var(--accent)",
              }}>
                {s.mode.toUpperCase()}
              </span>
            </td>
            <td>{s.open_now > 0 ? (
              <span className="badge" style={{ background: "rgba(63,185,80,0.15)", color: "var(--green)" }}>ACTIVE</span>
            ) : "—"}</td>
            <td>{s.applied_count}</td>
            <td className="pnl-positive">{s.success_count}</td>
            <td className="pnl-negative">{s.fail_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  return (
    <div>
      <h3 style={{ color: "var(--heading)", marginBottom: "0.5rem" }}>
        Static Strategies ({staticS.length})
      </h3>
      {renderTable(staticS)}

      {dynamicS.length > 0 && (
        <>
          <h3 style={{ color: "var(--heading)", marginBottom: "0.5rem" }}>
            Dynamic Strategies ({dynamicS.length})
            <span style={{ fontSize: "0.8rem", color: "var(--text-muted)", fontWeight: 400, marginLeft: 8 }}>
              auto-added from scanner
            </span>
          </h3>
          {renderTable(dynamicS)}
        </>
      )}
    </div>
  );
}
