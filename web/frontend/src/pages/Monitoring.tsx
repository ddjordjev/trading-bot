import { useEffect, useState, useCallback } from "react";

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

interface MetricsData {
  uptime_seconds: number;
  system: SystemMetrics;
  process: ProcessMetrics;
}

interface GrafanaConfig {
  port: number;
  dashboard_uid: string;
}

export function Monitoring() {
  const [data, setData] = useState<MetricsData | null>(null);
  const [grafana, setGrafana] = useState<GrafanaConfig | null>(null);
  const [grafanaReachable, setGrafanaReachable] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchMetrics = useCallback(() => {
    fetch("/api/system-metrics")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    fetchMetrics();
    const iv = setInterval(fetchMetrics, 5000);
    return () => clearInterval(iv);
  }, [fetchMetrics]);

  useEffect(() => {
    fetch("/api/grafana-url")
      .then((r) => r.json())
      .then((cfg) => {
        setGrafana(cfg);
        const img = new Image();
        img.onload = () => setGrafanaReachable(true);
        img.onerror = () => setGrafanaReachable(false);
        img.src = `${window.location.protocol}//${window.location.hostname}:${cfg.port}/public/img/grafana_icon.svg`;
        setTimeout(() => setGrafanaReachable((prev) => (prev === null ? false : prev)), 3000);
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

  const grafanaBase =
    grafana && grafanaReachable
      ? `${window.location.protocol}//${window.location.hostname}:${grafana.port}`
      : "";
  const panelIds = [1, 2, 3, 4, 6, 7, 10, 11, 12, 13];
  const soloPanelSrc = (panelId: number) =>
    `${grafanaBase}/d-solo/${grafana?.dashboard_uid}/trading-bot?orgId=1&theme=dark&panelId=${panelId}&refresh=5s`;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
          Auto-refreshing every 5s
        </span>
      </div>

      {grafanaBase ? (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
            gap: "0.75rem",
          }}
        >
          {panelIds.map((panelId) => (
            <iframe
              key={panelId}
              title={`Grafana Panel ${panelId}`}
              src={soloPanelSrc(panelId)}
              style={{
                width: "100%",
                height: "300px",
                border: "1px solid var(--border, #30363d)",
                borderRadius: "var(--radius, 8px)",
                background: "var(--bg-secondary, #161b22)",
              }}
            />
          ))}
        </div>
      ) : (
        <div className="empty-state">
          <p style={{ color: "var(--text-muted)" }}>Grafana not reachable yet.</p>
        </div>
      )}
    </div>
  );
}
