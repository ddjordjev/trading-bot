import { useEffect, useState } from "react";

interface GrafanaConfig {
  port: number;
  dashboard_uid: string;
}

export function Monitoring() {
  const [cfg, setCfg] = useState<GrafanaConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/grafana-url")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setCfg)
      .catch((e) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="empty-state">
        <p>Could not load Grafana config: {error}</p>
        <p style={{ fontSize: "0.8rem", color: "var(--text-muted)", marginTop: "0.5rem" }}>
          Make sure the bot is running and Grafana is configured.
        </p>
      </div>
    );
  }

  if (!cfg) {
    return (
      <div className="empty-state">
        <p style={{ color: "var(--text-muted)" }}>Loading Grafana...</p>
      </div>
    );
  }

  const base = `${window.location.protocol}//${window.location.hostname}:${cfg.port}`;
  const src = `${base}/d/${cfg.dashboard_uid}/trading-bot?orgId=1&kiosk&theme=dark`;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
      }}>
        <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
          Grafana Dashboard — auto-refreshing every 15s
        </span>
        <a
          href={base}
          target="_blank"
          rel="noopener noreferrer"
          style={{ fontSize: "0.8rem" }}
        >
          Open in new tab
        </a>
      </div>
      <div style={{
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        overflow: "hidden",
        background: "#0d1117",
      }}>
        <iframe
          src={src}
          title="Grafana Dashboard"
          style={{
            width: "100%",
            height: "calc(100vh - 180px)",
            border: "none",
            display: "block",
          }}
        />
      </div>
    </div>
  );
}
