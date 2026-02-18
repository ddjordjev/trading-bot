import { useEffect, useState } from "react";
import { get } from "../api/client";

interface TrendingCoin {
  symbol: string;
  name: string;
  price: number;
  volume_24h: number;
  market_cap: number;
  change_1h: number;
  change_24h: number;
  is_low_liquidity: boolean;
  has_dynamic_strategy: boolean;
}

export function Scanner() {
  const [coins, setCoins] = useState<TrendingCoin[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = () => {
    get<TrendingCoin[]>("/api/trending").then(setCoins).catch(() => {}).finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 30000);
    return () => clearInterval(id);
  }, []);

  if (loading) return <div className="empty-state">Loading...</div>;
  if (coins.length === 0) return <div className="empty-state">No trending coins detected</div>;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1rem" }}>
        <h3 style={{ color: "var(--heading)" }}>Trending Coins ({coins.length})</h3>
        <button className="btn-primary" onClick={refresh}>Refresh</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Price</th>
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
    </div>
  );
}
