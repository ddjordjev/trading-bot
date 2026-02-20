import { useEffect, useState, useCallback } from "react";
import { get } from "../api/client";

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
  const s = data.system;
  const p = data.process;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
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
            <MetricCard label="Server Uptime" value={formatUptime(data.uptime_seconds)} />
          </div>
        </div>
      </div>
    </div>
  );
}

interface DbTable {
  name: string;
  row_count: number;
}

interface DbTableData {
  columns: string[];
  rows: Record<string, unknown>[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

function DbExplorer() {
  const [tables, setTables] = useState<DbTable[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tableData, setTableData] = useState<DbTableData | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    get<DbTable[]>("/api/db/tables").then(setTables).catch(() => {});
  }, []);

  useEffect(() => {
    if (!selected) { setTableData(null); return; }
    setLoading(true);
    get<DbTableData>(`/api/db/table/${selected}?page=${page}&page_size=100`)
      .then(setTableData)
      .catch(() => setTableData(null))
      .finally(() => setLoading(false));
  }, [selected, page]);

  const selectTable = (name: string) => {
    setSelected(name);
    setPage(1);
  };

  return (
    <div style={{ marginTop: "1.5rem" }}>
      <h3 style={{
        margin: "0 0 0.75rem", fontSize: "0.85rem",
        color: "var(--text-muted, #8b949e)", textTransform: "uppercase", letterSpacing: "0.05em",
      }}>
        Database Explorer
      </h3>

      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", marginBottom: "0.75rem" }}>
        {tables.map(t => (
          <button
            key={t.name}
            onClick={() => selectTable(t.name)}
            style={{
              padding: "0.35rem 0.75rem", fontSize: "0.8rem", cursor: "pointer",
              borderRadius: "var(--radius, 6px)",
              border: `1px solid ${selected === t.name ? "var(--accent, #58a6ff)" : "var(--border, #30363d)"}`,
              background: selected === t.name ? "var(--accent, #58a6ff)" : "var(--bg-card, #161b22)",
              color: selected === t.name ? "#fff" : "var(--text, #c9d1d9)",
            }}
          >
            {t.name} <span style={{ opacity: 0.6 }}>({t.row_count})</span>
          </button>
        ))}
        {tables.length === 0 && (
          <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>No tables found</span>
        )}
      </div>

      {loading && <div style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>Loading...</div>}

      {tableData && !loading && (
        <div style={{
          background: "var(--bg-card, #161b22)", border: "1px solid var(--border, #30363d)",
          borderRadius: "var(--radius, 8px)", overflow: "hidden",
        }}>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.75rem" }}>
              <thead>
                <tr>
                  {tableData.columns.map(col => (
                    <th key={col} style={{
                      padding: "0.5rem 0.6rem", textAlign: "left", whiteSpace: "nowrap",
                      borderBottom: "1px solid var(--border, #30363d)",
                      color: "var(--text-muted, #8b949e)", fontWeight: 600, fontSize: "0.7rem",
                      textTransform: "uppercase", letterSpacing: "0.03em",
                      position: "sticky", top: 0, background: "var(--bg-card, #161b22)",
                    }}>
                      {col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {tableData.rows.map((row, i) => (
                  <tr key={i} style={{
                    borderBottom: "1px solid var(--border, #30363d)",
                    background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.02)",
                  }}>
                    {tableData.columns.map(col => (
                      <td key={col} style={{
                        padding: "0.4rem 0.6rem", whiteSpace: "nowrap",
                        maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis",
                      }}>
                        {row[col] == null ? <span style={{ opacity: 0.3 }}>NULL</span> : String(row[col])}
                      </td>
                    ))}
                  </tr>
                ))}
                {tableData.rows.length === 0 && (
                  <tr>
                    <td colSpan={tableData.columns.length} style={{
                      padding: "1rem", textAlign: "center", color: "var(--text-muted)",
                    }}>
                      Empty table
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {tableData.total_pages > 1 && (
            <div style={{
              display: "flex", justifyContent: "space-between", alignItems: "center",
              padding: "0.5rem 0.75rem", borderTop: "1px solid var(--border, #30363d)",
              fontSize: "0.75rem", color: "var(--text-muted)",
            }}>
              <span>{tableData.total} rows total</span>
              <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
                <button
                  disabled={page <= 1}
                  onClick={() => setPage(p => p - 1)}
                  style={{
                    padding: "0.2rem 0.5rem", fontSize: "0.75rem", cursor: page > 1 ? "pointer" : "default",
                    background: "none", border: "1px solid var(--border)", borderRadius: 4,
                    color: "var(--text-muted)", opacity: page <= 1 ? 0.3 : 1,
                  }}
                >
                  Prev
                </button>
                <span>Page {tableData.page} / {tableData.total_pages}</span>
                <button
                  disabled={page >= tableData.total_pages}
                  onClick={() => setPage(p => p + 1)}
                  style={{
                    padding: "0.2rem 0.5rem", fontSize: "0.75rem",
                    cursor: page < tableData.total_pages ? "pointer" : "default",
                    background: "none", border: "1px solid var(--border)", borderRadius: 4,
                    color: "var(--text-muted)", opacity: page >= tableData.total_pages ? 0.3 : 1,
                  }}
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function Monitoring() {
  const [data, setData] = useState<MetricsData | null>(null);
  const [grafana, setGrafana] = useState<GrafanaConfig | null>(null);
  const [grafanaReachable, setGrafanaReachable] = useState<boolean | null>(null);
  const [useGrafana, setUseGrafana] = useState(true);
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
        <DbExplorer />
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
      <DbExplorer />
    </div>
  );
}
