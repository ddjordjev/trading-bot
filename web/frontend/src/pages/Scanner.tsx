import { useEffect, useState, useCallback } from "react";
import { get } from "../api/client";
import { buildExchangeSymbolUrl } from "../utils/exchangeLinks";

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
  source?: string;
}

interface TradeQueueItem {
  symbol: string;
  side: string;
  strategy: string;
  strength: number;
  age_seconds: number;
  reason: string;
  supported_exchanges: string[];
}

interface StatusLite {
  trading_mode?: string;
}

export function Scanner() {
  const [coins, setCoins] = useState<TrendingCoin[]>([]);
  const [tradeQueue, setTradeQueue] = useState<TradeQueueItem[]>([]);
  const [tradingMode, setTradingMode] = useState("live");
  const [loading, setLoading] = useState(true);

  const refresh = () => {
    get<TrendingCoin[]>("/api/trending").then(setCoins).catch(() => {}).finally(() => setLoading(false));
  };

  const fetchTradeQueue = useCallback(() => {
    get<TradeQueueItem[]>("/api/trade-queue").then(setTradeQueue).catch(() => setTradeQueue([]));
  }, []);

  const fetchStatus = useCallback(() => {
    get<StatusLite>("/api/status")
      .then((s) => setTradingMode(String(s?.trading_mode || "live")))
      .catch(() => setTradingMode("live"));
  }, []);

  useEffect(() => {
    const loadingGuard = window.setTimeout(() => setLoading(false), 8000);
    refresh();
    fetchTradeQueue();
    fetchStatus();
    const coinIv = setInterval(refresh, 30000);
    const queueIv = setInterval(fetchTradeQueue, 5000);
    const statusIv = setInterval(fetchStatus, 30000);
    return () => {
      window.clearTimeout(loadingGuard);
      clearInterval(coinIv);
      clearInterval(queueIv);
      clearInterval(statusIv);
    };
  }, [fetchTradeQueue, fetchStatus]);

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
                <th>Exchanges</th>
                <th>Age</th>
              </tr>
            </thead>
            <tbody>
              {tradeQueue.map((p, i) => (
                  <tr key={`${p.symbol}-${p.side}-${i}`}>
                    <td style={{ fontWeight: 600 }}>
                      {(() => {
                        const primaryExchange = (p.supported_exchanges || [])[0];
                        const href = buildExchangeSymbolUrl(p.symbol, primaryExchange, tradingMode);
                        if (!href) return p.symbol;
                        return (
                          <a
                            href={href}
                            target="_blank"
                            rel="noopener noreferrer"
                            style={{ color: "var(--accent)", textDecoration: "none", fontWeight: 700 }}
                            title={primaryExchange ? `Open on ${primaryExchange}` : "Open symbol"}
                          >
                            {p.symbol} ↗
                          </a>
                        );
                      })()}
                    </td>
                    <td style={{ color: p.side === "long" || p.side === "buy" ? "var(--green)" : "var(--red)" }}>
                      {p.side === "buy" ? "LONG" : p.side === "sell" ? "SHORT" : p.side.toUpperCase()}
                    </td>
                    <td>{p.strategy || "—"}</td>
                    <td>{p.strength.toFixed(2)}</td>
                    <td style={{ fontSize: "0.8rem" }}>{(p.supported_exchanges || []).join(", ") || "—"}</td>
                    <td>{p.age_seconds < 60 ? `${Math.round(p.age_seconds)}s` : `${(p.age_seconds / 60).toFixed(1)}m`}</td>
                  </tr>
              ))}
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
                  {c.source === "binance_scanner" ? (
                    <a
                      href={buildExchangeSymbolUrl(c.symbol, "BINANCE", tradingMode)}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ color: "var(--accent)", textDecoration: "none", fontWeight: 700 }}
                      title="Open on BINANCE"
                    >
                      {c.symbol} ↗
                    </a>
                  ) : (
                    <strong>{c.symbol}</strong>
                  )}
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
