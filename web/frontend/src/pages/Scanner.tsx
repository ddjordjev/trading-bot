import { useEffect, useState, useCallback } from "react";
import { get } from "../api/client";

interface TrendingCoin {
  symbol: string;
  name: string;
  price: number;
  volume_24h: number;
  market_cap: number;
  change_5m: number;
  change_1h: number;
  change_24h: number;
  is_low_liquidity: boolean;
  has_dynamic_strategy: boolean;
}

interface TradeQueueItem {
  symbol: string;
  side: string;
  strategy: string;
  strength: number;
  age_seconds: number;
  status: string;
  reason: string;
}

export function Scanner() {
  const [coins, setCoins] = useState<TrendingCoin[]>([]);
  const [tradeQueue, setTradeQueue] = useState<TradeQueueItem[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = () => {
    get<TrendingCoin[]>("/api/trending").then(setCoins).catch(() => {}).finally(() => setLoading(false));
  };

  const fetchTradeQueue = useCallback(() => {
    get<TradeQueueItem[]>("/api/trade-queue").then(setTradeQueue).catch(() => setTradeQueue([]));
  }, []);

  useEffect(() => {
    refresh();
    fetchTradeQueue();
    const coinIv = setInterval(refresh, 30000);
    const queueIv = setInterval(fetchTradeQueue, 5000);
    return () => { clearInterval(coinIv); clearInterval(queueIv); };
  }, [fetchTradeQueue]);

  if (loading) return <div className="empty-state">Loading...</div>;

  return (
    <div>
      <h3 style={{ color: "var(--heading)", marginBottom: "0.5rem" }}>
        Trade Queue ({tradeQueue.length})
      </h3>
      {tradeQueue.length === 0 ? (
        <div className="empty-state" style={{ marginBottom: "1.5rem" }}>No proposals yet</div>
      ) : (
        <div style={{ overflowX: "auto", marginBottom: "1.5rem" }}>
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th>Strategy</th>
                <th>Strength</th>
                <th>Age</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {tradeQueue.map((p, i) => {
                const statusStyle: Record<string, { bg: string; fg: string }> = {
                  pending:  { bg: "rgba(88,166,255,0.15)", fg: "var(--accent)" },
                  consumed: { bg: "rgba(63,185,80,0.15)",  fg: "var(--green)" },
                  rejected: { bg: "rgba(248,81,73,0.15)",  fg: "var(--red)" },
                  expired:  { bg: "rgba(139,148,158,0.15)", fg: "var(--text-muted)" },
                };
                const st = statusStyle[p.status] || statusStyle.expired;
                const rowOpacity = p.status === "expired" || p.status === "rejected" ? 0.55 : 1;
                return (
                  <tr key={`${p.symbol}-${p.side}-${i}`} style={{ opacity: rowOpacity }}>
                    <td style={{ fontWeight: 600 }}>{p.symbol}</td>
                    <td style={{ color: p.side === "long" || p.side === "buy" ? "var(--green)" : "var(--red)" }}>
                      {p.side === "buy" ? "LONG" : p.side === "sell" ? "SHORT" : p.side.toUpperCase()}
                    </td>
                    <td>{p.strategy || "—"}</td>
                    <td>{p.strength.toFixed(2)}</td>
                    <td>{p.age_seconds < 60 ? `${Math.round(p.age_seconds)}s` : `${(p.age_seconds / 60).toFixed(1)}m`}</td>
                    <td>
                      <span className="badge" style={{ background: st.bg, color: st.fg }}
                        title={p.reason || ""}>{p.status.toUpperCase()}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1rem" }}>
        <h3 style={{ color: "var(--heading)" }}>Trending Coins ({coins.length})</h3>
        <button className="btn-primary" onClick={refresh}>Refresh</button>
      </div>
      {coins.length === 0 ? (
        <div className="empty-state">No trending coins detected</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Price</th>
              <th>5m Change</th>
              <th>1h Change</th>
              <th>24h Change</th>
              <th>Volume 24h</th>
              <th>Market Cap</th>
              <th>Liquidity</th>
              <th>Strategy</th>
            </tr>
          </thead>
          <tbody>
            {coins.map((c) => (
              <tr key={c.symbol}>
                <td>
                  <strong>{c.symbol}</strong>
                  {c.name && <span style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginLeft: 4 }}>{c.name}</span>}
                </td>
                <td>${c.price < 1 ? c.price.toFixed(6) : c.price.toFixed(2)}</td>
                <td className={c.change_5m >= 0 ? "pnl-positive" : "pnl-negative"}>
                  {c.change_5m >= 0 ? "+" : ""}{c.change_5m.toFixed(2)}%
                </td>
                <td className={c.change_1h >= 0 ? "pnl-positive" : "pnl-negative"}>
                  {c.change_1h >= 0 ? "+" : ""}{c.change_1h.toFixed(2)}%
                </td>
                <td className={c.change_24h >= 0 ? "pnl-positive" : "pnl-negative"}>
                  {c.change_24h >= 0 ? "+" : ""}{c.change_24h.toFixed(2)}%
                </td>
                <td>${(c.volume_24h / 1e6).toFixed(1)}M</td>
                <td>${(c.market_cap / 1e6).toFixed(0)}M</td>
                <td>
                  {c.is_low_liquidity ? (
                    <span className="badge" style={{ background: "rgba(248,81,73,0.15)", color: "var(--red)" }}>LOW</span>
                  ) : (
                    <span className="badge" style={{ background: "rgba(63,185,80,0.15)", color: "var(--green)" }}>OK</span>
                  )}
                </td>
                <td>
                  {c.has_dynamic_strategy ? (
                    <span className="badge" style={{ background: "rgba(88,166,255,0.15)", color: "var(--accent)" }}>ACTIVE</span>
                  ) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
