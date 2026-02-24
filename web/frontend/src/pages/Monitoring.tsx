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

function CircularGauge({ label, value, color }: { label: string; value: number; color: string }) {
  const clamped = Math.max(0, Math.min(100, value));
  const radius = 46;
  const stroke = 8;
  const circumference = 2 * Math.PI * radius;
  const dashOffset = circumference * (1 - clamped / 100);

  return (
    <div
      style={{
        background: "var(--bg-card, #161b22)",
        border: "1px solid var(--border, #30363d)",
        borderRadius: "var(--radius, 8px)",
        padding: "0.9rem",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: "0.4rem",
      }}
    >
      <div style={{ fontSize: "0.75rem", color: "var(--text-muted, #8b949e)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
        {label}
      </div>
      <svg width="120" height="120" viewBox="0 0 120 120" role="img" aria-label={`${label} ${clamped}%`}>
        <circle cx="60" cy="60" r={radius} fill="none" stroke="var(--border, #30363d)" strokeWidth={stroke} />
        <circle
          cx="60"
          cy="60"
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={dashOffset}
          transform="rotate(-90 60 60)"
          style={{ transition: "stroke-dashoffset 0.5s ease" }}
        />
        <text x="60" y="66" textAnchor="middle" fontSize="20" fontWeight="700" fill="var(--text, #c9d1d9)">
          {Math.round(clamped)}%
        </text>
      </svg>
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
  const cpuColor = s.cpu_pct > 80 ? "#f85149" : s.cpu_pct > 50 ? "#d29922" : "#3fb950";
  const memColor = s.mem_pct > 85 ? "#f85149" : s.mem_pct > 60 ? "#d29922" : "#3fb950";
  const diskColor = s.disk_pct > 90 ? "#f85149" : s.disk_pct > 70 ? "#d29922" : "#3fb950";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: "0.75rem" }}>
        <CircularGauge label="CPU" value={s.cpu_pct} color={cpuColor} />
        <CircularGauge label="Memory" value={s.mem_pct} color={memColor} />
        <CircularGauge label="Disk" value={s.disk_pct} color={diskColor} />
      </div>

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
              <ProgressBar pct={s.cpu_pct} color={cpuColor} />
            </div>
            <div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8rem", marginBottom: "0.25rem" }}>
                <span>Memory</span><span>{s.mem_used_gb} / {s.mem_total_gb} GB ({s.mem_pct}%)</span>
              </div>
              <ProgressBar pct={s.mem_pct} color={memColor} />
            </div>
            <div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8rem", marginBottom: "0.25rem" }}>
                <span>Disk</span><span>{s.disk_used_gb} GB used, {s.disk_free_gb} GB free ({s.disk_pct}%)</span>
              </div>
              <ProgressBar pct={s.disk_pct} color={diskColor} />
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

export function Monitoring() {
  const [data, setData] = useState<MetricsData | null>(null);
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

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
          Auto-refreshing every 5s
        </span>
      </div>
      <BuiltInDashboard data={data} />
    </div>
  );
}
