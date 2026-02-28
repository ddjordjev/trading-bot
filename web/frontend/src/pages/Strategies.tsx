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

export function Strategies() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const loadingGuard = window.setTimeout(() => setLoading(false), 8000);
    get<Strategy[]>("/api/strategies")
      .then(setStrategies)
      .catch(() => {})
      .finally(() => setLoading(false));
    return () => window.clearTimeout(loadingGuard);
  }, []);

  if (loading) return <div className="empty-state">Loading...</div>;
  if (strategies.length === 0) return <div className="empty-state">No active strategies</div>;

  const staticS = strategies.filter((s) => !s.is_dynamic);
  const dynamicS = strategies.filter((s) => s.is_dynamic);

  const renderTable = (rows: Strategy[]) => (
    <table style={{ marginBottom: "2rem" }}>
      <thead>
        <tr>
          {/* Bot column removed — strategies aggregated across all bots */}
          <th>Strategy</th>
          <th>Symbols</th>
          <th>Market</th>
          <th>Leverage</th>
          <th>Mode</th>
          <th>Open</th>
          <th>Trades</th>
          <th>Wins</th>
          <th>Losses</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((s, i) => {
          const symbols = s.symbol ? s.symbol.split(",").map((x) => x.trim()).filter(Boolean) : [];
          const totalTrades = s.success_count + s.fail_count;
          const winRate = totalTrades > 0 ? Math.round((s.success_count / totalTrades) * 100) : 0;
          return (
            <tr key={`${s.name}:${i}`}>
              <td><strong>{s.name}</strong></td>
              <td title={s.symbol || "none"}>
                {symbols.length > 0 ? (
                  <span style={{ fontSize: "0.8rem" }}>
                    {symbols.length} pair{symbols.length !== 1 ? "s" : ""}
                  </span>
                ) : "—"}
              </td>
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
                <span className="badge" style={{ background: "rgba(63,185,80,0.15)", color: "var(--green)" }}>
                  {s.open_now} open
                </span>
              ) : "—"}</td>
              <td>
                {s.applied_count}
                {totalTrades > 0 && (
                  <span style={{ fontSize: "0.7rem", color: "var(--text-muted)", marginLeft: 4 }}>
                    ({winRate}% WR)
                  </span>
                )}
              </td>
              <td className="pnl-positive">{s.success_count}</td>
              <td className="pnl-negative">{s.fail_count}</td>
            </tr>
          );
        })}
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
