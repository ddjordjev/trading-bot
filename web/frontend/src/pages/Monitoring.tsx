import { useEffect, useState, useCallback } from "react";

interface TradingMetrics {
  balance: number;
  daily_pnl: number;
  daily_pnl_pct: number;
  daily_loss_pct: number;
  open_positions: number;
  trades_today: number;
  win_rate: number;
  strategies: number;
  fear_greed: number;
}

interface SystemMetrics {
  cpu_pct: number;
  mem_used_gb: number;
  mem_total_gb: number;
  mem_pct: number;
  disk_used_gb: number;
  disk_free_gb: number;
  disk_pct: number;
}

interface ProcessMetrics {
  cpu_pct: number;
  rss_mb: number;
  threads: number;
  uptime_hours: number;
}

interface Position {
  symbol: string;
  side: string;
  amount: number;
  entry: number;
  leverage: number;
  adds: number;
}

interface MetricsData {
  uptime_seconds: number;
  trading: TradingMetrics;
  positions: Position[];
  system: SystemMetrics;
  process: ProcessMetrics;
}

interface GrafanaConfig {
  port: number;
  dashboard_uid: string;
}

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function MetricCard({ label, value, sub, color }: {
  label: string; value: string; sub?: string; color?: string;
}) {
  return (
    <div style={{
      background: "var(--bg-card, #161b22)",
      border: "1px solid var(--border, #30363d)",
      borderRadius: "var(--radius, 8px)",
      padding: "1rem",
      minWidth: 0,
    }}>
      <div style={{ fontSize: "0.75rem", color: "var(--text-muted, #8b949e)", marginBottom: "0.25rem" }}>
        {label}
      </div>
      <div style={{ fontSize: "1.5rem", fontWeight: 600, color: color || "var(--text, #c9d1d9)" }}>
        {value}
      </div>
      {sub && (
        <div style={{ fontSize: "0.7rem", color: "var(--text-muted, #8b949e)", marginTop: "0.15rem" }}>
          {sub}
        </div>
      )}
    </div>
  );
}

function ProgressBar({ pct, color }: { pct: number; color: string }) {
  return (
    <div style={{
      height: 6, borderRadius: 3, background: "var(--border, #30363d)", overflow: "hidden",
    }}>
      <div style={{
        width: `${Math.min(100, pct)}%`, height: "100%", borderRadius: 3,
        background: color, transition: "width 0.5s",
      }} />
    </div>
  );
}

function BuiltInDashboard({ data }: { data: MetricsData }) {
  const t = data.trading;
  const s = data.system;
  const p = data.process;
  const pnlColor = t.daily_pnl >= 0 ? "#3fb950" : "#f85149";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      {/* Trading */}
      <div>
        <h3 style={{ margin: "0 0 0.75rem", fontSize: "0.85rem", color: "var(--text-muted, #8b949e)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
          Trading
        </h3>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))", gap: "0.75rem" }}>
          <MetricCard label="Balance" value={`$${t.balance.toFixed(2)}`} />
          <MetricCard
            label="Daily PnL"
            value={`${t.daily_pnl >= 0 ? "+" : ""}$${t.daily_pnl.toFixed(2)}`}
            sub={`${t.daily_pnl_pct >= 0 ? "+" : ""}${t.daily_pnl_pct}%`}
            color={pnlColor}
          />
          <MetricCard label="Positions" value={String(t.open_positions)} sub={`of ${t.strategies} strategies`} />
          <MetricCard label="Trades Today" value={String(t.trades_today)} sub={`${t.win_rate}% win rate`} />
          <MetricCard label="Fear & Greed" value={String(t.fear_greed)} sub={t.fear_greed <= 25 ? "Extreme Fear" : t.fear_greed <= 45 ? "Fear" : t.fear_greed <= 55 ? "Neutral" : t.fear_greed <= 75 ? "Greed" : "Extreme Greed"} color={t.fear_greed <= 25 ? "#f85149" : t.fear_greed >= 75 ? "#3fb950" : undefined} />
          <MetricCard label="Uptime" value={formatUptime(data.uptime_seconds)} />
        </div>
      </div>

      {/* Open Positions */}
      {data.positions.length > 0 && (
        <div>
          <h3 style={{ margin: "0 0 0.75rem", fontSize: "0.85rem", color: "var(--text-muted, #8b949e)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
            Open Positions
          </h3>
          <div style={{
            background: "var(--bg-card, #161b22)",
            border: "1px solid var(--border, #30363d)",
            borderRadius: "var(--radius, 8px)",
            overflow: "hidden",
          }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border, #30363d)" }}>
                  {["Symbol", "Side", "Amount", "Entry", "Leverage", "DCA Adds"].map(h => (
                    <th key={h} style={{ padding: "0.5rem 0.75rem", textAlign: "left", color: "var(--text-muted, #8b949e)", fontWeight: 500, fontSize: "0.75rem" }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.positions.map(pos => (
                  <tr key={pos.symbol} style={{ borderBottom: "1px solid var(--border, #30363d)" }}>
                    <td style={{ padding: "0.5rem 0.75rem", fontWeight: 600 }}>{pos.symbol}</td>
                    <td style={{ padding: "0.5rem 0.75rem", color: pos.side === "long" ? "#3fb950" : "#f85149" }}>
                      {pos.side.toUpperCase()}
                    </td>
                    <td style={{ padding: "0.5rem 0.75rem" }}>{pos.amount.toFixed(4)}</td>
                    <td style={{ padding: "0.5rem 0.75rem" }}>${pos.entry.toFixed(2)}</td>
                    <td style={{ padding: "0.5rem 0.75rem" }}>{pos.leverage}x</td>
                    <td style={{ padding: "0.5rem 0.75rem" }}>{pos.adds}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* System & Process */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1.25rem" }}>
        <div>
          <h3 style={{ margin: "0 0 0.75rem", fontSize: "0.85rem", color: "var(--text-muted, #8b949e)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
            System
          </h3>
          <div style={{
            background: "var(--bg-card, #161b22)",
            border: "1px solid var(--border, #30363d)",
            borderRadius: "var(--radius, 8px)",
            padding: "1rem",
            display: "flex", flexDirection: "column", gap: "0.75rem",
          }}>
            <div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8rem", marginBottom: "0.25rem" }}>
                <span>CPU</span><span>{s.cpu_pct}%</span>
              </div>
              <ProgressBar pct={s.cpu_pct} color={s.cpu_pct > 80 ? "#f85149" : s.cpu_pct > 50 ? "#d29922" : "#3fb950"} />
            </div>
            <div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8rem", marginBottom: "0.25rem" }}>
                <span>Memory</span><span>{s.mem_used_gb} / {s.mem_total_gb} GB ({s.mem_pct}%)</span>
              </div>
              <ProgressBar pct={s.mem_pct} color={s.mem_pct > 85 ? "#f85149" : s.mem_pct > 60 ? "#d29922" : "#3fb950"} />
            </div>
            <div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8rem", marginBottom: "0.25rem" }}>
                <span>Disk</span><span>{s.disk_used_gb} GB used, {s.disk_free_gb} GB free ({s.disk_pct}%)</span>
              </div>
              <ProgressBar pct={s.disk_pct} color={s.disk_pct > 90 ? "#f85149" : s.disk_pct > 70 ? "#d29922" : "#3fb950"} />
            </div>
          </div>
        </div>

        <div>
          <h3 style={{ margin: "0 0 0.75rem", fontSize: "0.85rem", color: "var(--text-muted, #8b949e)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
            Process
          </h3>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.75rem" }}>
            <MetricCard label="Process CPU" value={`${p.cpu_pct}%`} />
            <MetricCard label="Memory (RSS)" value={`${p.rss_mb} MB`} />
            <MetricCard label="Threads" value={String(p.threads)} />
            <MetricCard label="Process Uptime" value={`${p.uptime_hours}h`} />
          </div>
        </div>
      </div>
    </div>
  );
}

export function Monitoring() {
  const [data, setData] = useState<MetricsData | null>(null);
  const [grafana, setGrafana] = useState<GrafanaConfig | null>(null);
  const [grafanaReachable, setGrafanaReachable] = useState<boolean | null>(null);
  const [useGrafana, setUseGrafana] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchMetrics = useCallback(() => {
    fetch("/api/system-metrics")
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(setData)
      .catch(e => setError(e.message));
  }, []);

  useEffect(() => {
    fetchMetrics();
    const iv = setInterval(fetchMetrics, 5000);
    return () => clearInterval(iv);
  }, [fetchMetrics]);

  useEffect(() => {
    fetch("/api/grafana-url")
      .then(r => r.json())
      .then(cfg => {
        setGrafana(cfg);
        const img = new Image();
        img.onload = () => setGrafanaReachable(true);
        img.onerror = () => setGrafanaReachable(false);
        img.src = `${window.location.protocol}//${window.location.hostname}:${cfg.port}/public/img/grafana_icon.svg`;
        setTimeout(() => setGrafanaReachable(prev => prev === null ? false : prev), 3000);
      })
      .catch(() => setGrafanaReachable(false));
  }, []);

  if (error && !data) {
    return (
      <div className="empty-state">
        <p>Could not load metrics: {error}</p>
        <p style={{ fontSize: "0.8rem", color: "var(--text-muted)", marginTop: "0.5rem" }}>
          Make sure the bot is running.
        </p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="empty-state">
        <p style={{ color: "var(--text-muted)" }}>Loading metrics...</p>
      </div>
    );
  }

  if (useGrafana && grafana) {
    const base = `${window.location.protocol}//${window.location.hostname}:${grafana.port}`;
    const src = `${base}/d/${grafana.dashboard_uid}/trading-bot?orgId=1&kiosk&theme=dark`;
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <button
            onClick={() => setUseGrafana(false)}
            style={{ fontSize: "0.8rem", background: "none", border: "1px solid var(--border)", borderRadius: 4, padding: "0.25rem 0.5rem", color: "var(--text-muted)", cursor: "pointer" }}
          >
            Back to built-in
          </button>
          <a href={base} target="_blank" rel="noopener noreferrer" style={{ fontSize: "0.8rem" }}>
            Open Grafana
          </a>
        </div>
        <div style={{
          border: "1px solid var(--border)", borderRadius: "var(--radius)",
          overflow: "hidden", background: "#0d1117",
        }}>
          <iframe src={src} title="Grafana Dashboard" style={{
            width: "100%", height: "calc(100vh - 180px)", border: "none", display: "block",
          }} />
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
          Auto-refreshing every 5s
        </span>
        {grafanaReachable && (
          <button
            onClick={() => setUseGrafana(true)}
            style={{ fontSize: "0.8rem", background: "none", border: "1px solid var(--border)", borderRadius: 4, padding: "0.25rem 0.5rem", color: "var(--text-muted)", cursor: "pointer" }}
          >
            Switch to Grafana
          </button>
        )}
      </div>
      <BuiltInDashboard data={data} />
    </div>
  );
}
