import { useEffect, useState } from "react";
import type { IntelSnapshot } from "../hooks/useWebSocket";
import { get } from "../api/client";
import { FearGreedGauge } from "../components/FearGreedGauge";

interface NewsItem {
  headline: string;
  source: string;
  url: string;
  published: string;
  matched_symbols: string[];
  sentiment: string;
  sentiment_score: number;
}

const SENTIMENT_COLORS: Record<string, string> = {
  bullish: "var(--green)",
  bearish: "var(--red)",
  neutral: "var(--text-muted)",
};

const REGIME_COLORS: Record<string, string> = {
  risk_on: "var(--green)",
  normal: "var(--text)",
  caution: "var(--yellow)",
  risk_off: "var(--red)",
  capitulation: "var(--purple)",
};

const LIQUIDATION_BIAS_COLORS: Record<string, string> = {
  long: "var(--green)",
  short: "var(--red)",
  neutral: "var(--text-muted)",
};

function formatUsdCompact(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (value >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
  if (value >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
  if (value >= 1e3) return `$${(value / 1e3).toFixed(1)}K`;
  return `$${Math.round(value).toLocaleString()}`;
}

export function Intel({ wsIntel }: { wsIntel: IntelSnapshot | null }) {
  const [intel, setIntel] = useState<IntelSnapshot | null>(wsIntel);
  const [news, setNews] = useState<NewsItem[]>([]);

  useEffect(() => {
    if (wsIntel) setIntel(wsIntel);
  }, [wsIntel]);

  useEffect(() => {
    if (!intel) {
      get<IntelSnapshot | null>("/api/intel").then(setIntel).catch(() => {});
    }
    const fetchNews = () => get<NewsItem[]>("/api/news").then(setNews).catch(() => {});
    fetchNews();
    const iv = setInterval(fetchNews, 30000);
    return () => clearInterval(iv);
  }, []);

  if (!intel) return <div className="empty-state">Intel module not active</div>;

  const regimeColor = REGIME_COLORS[intel.regime] || "var(--text)";
  const liquidationAvailable = Array.isArray(intel.sources_active) && intel.sources_active.includes("liquidations");

  return (
    <div>
      <div className="stat-grid">
        <div className="stat-card">
          <div className="label">Market Regime</div>
          <div className="value" style={{ color: regimeColor }}>
            {intel.regime.replace("_", " ").toUpperCase()}
          </div>
        </div>
        <div className="stat-card">
          <div className="label">Direction Bias</div>
          <div className="value" style={{
            color: intel.preferred_direction === "long" ? "var(--green)"
              : intel.preferred_direction === "short" ? "var(--red)" : "var(--text-muted)"
          }}>
            {intel.preferred_direction.toUpperCase()}
          </div>
        </div>
        <div className="stat-card">
          <div className="label">Size Multiplier</div>
          <div className="value">{intel.position_size_multiplier.toFixed(2)}x</div>
        </div>
        <div className="stat-card">
          <div className="label">Reduce Exposure</div>
          <div className="value" style={{ color: intel.should_reduce_exposure ? "var(--red)" : "var(--green)" }}>
            {intel.should_reduce_exposure ? "YES" : "NO"}
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem", marginBottom: "1.5rem" }}>
        <FearGreedGauge value={intel.fear_greed} />

        <div className="card">
          <h3 style={{ color: "var(--heading)", marginBottom: "0.8rem" }}>Liquidations (24h)</h3>
          <div style={{ fontSize: "1.5rem", fontWeight: 700, color: "var(--heading)" }}>
            {liquidationAvailable ? formatUsdCompact(intel.liquidation_24h) : "Data unavailable"}
          </div>
          {intel.mass_liquidation && (
            <div style={{
              background: "rgba(248,81,73,0.15)", border: "1px solid var(--red)",
              borderRadius: "var(--radius)", padding: "0.5rem", marginTop: "0.5rem",
              color: "var(--red)", fontWeight: 600,
            }}>
              MASS LIQUIDATION EVENT
            </div>
          )}
          <div style={{ marginTop: "0.5rem", color: "var(--text-muted)", fontSize: "0.85rem" }}>
            Bias: <strong style={{ color: LIQUIDATION_BIAS_COLORS[intel.liquidation_bias] || "var(--text)" }}>
              {intel.liquidation_bias.toUpperCase()}
            </strong>
          </div>
          {!liquidationAvailable && (
            <div style={{ marginTop: "0.4rem", color: "var(--text-muted)", fontSize: "0.78rem" }}>
              Source feed inactive (CoinGlass API)
            </div>
          )}
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
        <div className="card">
          <h3 style={{ color: "var(--heading)", marginBottom: "0.8rem" }}>Macro Calendar</h3>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.5rem" }}>
            <span>Event Imminent</span>
            <strong style={{ color: intel.macro_event_imminent ? "var(--red)" : "var(--green)" }}>
              {intel.macro_event_imminent ? "YES" : "No"}
            </strong>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.5rem" }}>
            <span>Exposure Multiplier</span>
            <strong>{intel.macro_exposure_mult.toFixed(2)}x</strong>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.5rem" }}>
            <span>Spike Opportunity</span>
            <strong style={{ color: intel.macro_spike_opportunity ? "var(--green)" : "var(--text-muted)" }}>
              {intel.macro_spike_opportunity ? "YES" : "No"}
            </strong>
          </div>
          {intel.next_macro_event && (
            <div style={{ marginTop: "0.5rem", padding: "0.5rem", background: "var(--bg)", borderRadius: "var(--radius)", fontSize: "0.8rem" }}>
              Next: {intel.next_macro_event}
            </div>
          )}
          {intel.macro_events && intel.macro_events.length > 0 && (
            <div style={{ marginTop: "0.8rem" }}>
              <h4 style={{ color: "var(--heading)", fontSize: "0.8rem", marginBottom: "0.4rem" }}>
                Upcoming High-Impact ({intel.macro_events.length})
              </h4>
              <div style={{ maxHeight: "220px", overflowY: "auto" }}>
                <table style={{ width: "100%", fontSize: "0.75rem", borderCollapse: "collapse" }}>
                  <thead>
                    <tr style={{ color: "var(--text-muted)", borderBottom: "1px solid var(--border)" }}>
                      <th style={{ textAlign: "left", padding: "0.3rem 0.4rem" }}>Event</th>
                      <th style={{ textAlign: "center", padding: "0.3rem 0.4rem" }}>Impact</th>
                      <th style={{ textAlign: "right", padding: "0.3rem 0.4rem" }}>When</th>
                    </tr>
                  </thead>
                  <tbody>
                    {intel.macro_events.map((ev, i) => {
                      const impactColor = ev.impact === "critical" ? "var(--red)"
                        : ev.impact === "high" ? "var(--yellow)" : "var(--text-muted)";
                      const h = ev.hours_until;
                      const when = h > 0
                        ? (h >= 24 ? `in ${(h / 24).toFixed(0)}d` : `in ${h.toFixed(0)}h`)
                        : (h > -1 ? "NOW" : `${Math.abs(h).toFixed(0)}h ago`);
                      const rowBg = h > -1 && h <= 2 ? "rgba(248,81,73,0.08)" : "transparent";
                      return (
                        <tr key={i} style={{ background: rowBg, borderBottom: "1px solid var(--border)" }}>
                          <td style={{ padding: "0.3rem 0.4rem" }}>{ev.title}</td>
                          <td style={{ padding: "0.3rem 0.4rem", textAlign: "center" }}>
                            <span style={{ color: impactColor, fontWeight: 600, textTransform: "uppercase", fontSize: "0.65rem" }}>
                              {ev.impact}
                            </span>
                          </td>
                          <td style={{ padding: "0.3rem 0.4rem", textAlign: "right", fontWeight: h > -1 && h <= 2 ? 700 : 400, color: h > -1 && h <= 2 ? "var(--red)" : "var(--text)" }}>
                            {when}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>

        <div className="card">
          <h3 style={{ color: "var(--heading)", marginBottom: "0.8rem" }}>Whale Sentiment</h3>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.5rem" }}>
            <span>Contrarian Bias</span>
            <strong style={{
              color: intel.whale_bias === "long" ? "var(--green)"
                : intel.whale_bias === "short" ? "var(--red)" : "var(--text-muted)"
            }}>
              {intel.whale_bias.toUpperCase()}
            </strong>
          </div>
          {intel.overleveraged_side && (
            <div style={{
              background: "rgba(210,153,34,0.15)", border: "1px solid var(--yellow)",
              borderRadius: "var(--radius)", padding: "0.5rem", marginTop: "0.5rem",
              color: "var(--yellow)", fontSize: "0.85rem",
            }}>
              Overleveraged: {intel.overleveraged_side.toUpperCase()}
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: "1rem" }}>
        <h3 style={{ color: "var(--heading)", marginBottom: "0.8rem" }}>
          News Feed ({news.length})
        </h3>
        {news.length === 0 ? (
          <div style={{ color: "var(--text-muted)", fontSize: "0.85rem" }}>
            No news items yet. Feed refreshes every 30s.
          </div>
        ) : (
          <div style={{ maxHeight: 350, overflowY: "auto" }}>
            {news.map((n, i) => (
              <div key={i} style={{
                padding: "0.5rem 0",
                borderBottom: i < news.length - 1 ? "1px solid var(--border)" : "none",
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "0.5rem" }}>
                  <div style={{ flex: 1 }}>
                    {n.url ? (
                      <a href={n.url} target="_blank" rel="noopener noreferrer"
                        style={{ color: "var(--heading)", textDecoration: "none", fontWeight: 500, fontSize: "0.85rem" }}>
                        {n.headline}
                      </a>
                    ) : (
                      <span style={{ color: "var(--heading)", fontWeight: 500, fontSize: "0.85rem" }}>{n.headline}</span>
                    )}
                    <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.25rem", flexWrap: "wrap", alignItems: "center" }}>
                      <span style={{ fontSize: "0.7rem", color: "var(--text-muted)" }}>{n.source}</span>
                      {n.published && (
                        <span style={{ fontSize: "0.7rem", color: "var(--text-muted)" }}>
                          {new Date(n.published).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                        </span>
                      )}
                      {n.matched_symbols.map((s) => (
                        <span key={s} className="badge" style={{ fontSize: "0.65rem", padding: "0.1rem 0.3rem", background: "rgba(88,166,255,0.15)", color: "var(--accent)" }}>
                          {s}
                        </span>
                      ))}
                    </div>
                  </div>
                  <span style={{
                    fontSize: "0.7rem", fontWeight: 600, whiteSpace: "nowrap",
                    color: SENTIMENT_COLORS[n.sentiment] || "var(--text-muted)",
                  }}>
                    {n.sentiment.toUpperCase()}
                    {n.sentiment !== "neutral" && ` ${(n.sentiment_score * 100).toFixed(0)}%`}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
