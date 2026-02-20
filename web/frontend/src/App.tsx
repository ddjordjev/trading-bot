import { useState, useEffect, useMemo } from "react";
import { useMultiBotWebSocket } from "./hooks/useMultiBotWebSocket";
import type { MergedDashboard } from "./hooks/useMultiBotWebSocket";
import type { FullSnapshot } from "./hooks/useWebSocket";
import { Dashboard } from "./pages/Dashboard";
import { Intel } from "./pages/Intel";
import { Scanner } from "./pages/Scanner";
import { Strategies } from "./pages/Strategies";
import { Analytics } from "./pages/Analytics";
import { Monitoring } from "./pages/Monitoring";
import { Settings } from "./pages/Settings";
import { get } from "./api/client";

interface BotInfo {
  bot_id: string;
  label: string;
  port: number;
  strategies: string[];
}

const TABS = [
  "Dashboard", "Intel", "Scanner", "Strategies", "Analytics", "Monitoring", "Settings",
] as const;
type Tab = typeof TABS[number];

export function App() {
  const [bots, setBots] = useState<BotInfo[]>([]);
  const [selected, setSelected] = useState("all");
  const [tab, setTab] = useState<Tab>("Dashboard");

  useEffect(() => {
    get<BotInfo[]>("/api/bots")
      .then((list) => setBots(list))
      .catch(() => {});
  }, []);

  const botPorts = useMemo(
    () => bots.map((b) => ({ bot_id: b.bot_id, port: b.port })),
    [bots],
  );

  const { merged, allConnected } = useMultiBotWebSocket(botPorts);

  const dashboardData: FullSnapshot | null = useMemo(() => {
    if (!merged) return null;
    if (selected === "all") {
      return {
        status: merged.status,
        positions: merged.positions,
        wick_scalps: merged.wick_scalps,
        logs: merged.logs,
        intel: merged.intel,
      };
    }
    const bot = merged.bots.find((b) => b.bot_id === selected);
    if (!bot?.data) return null;
    return {
      ...bot.data,
      positions: bot.data.positions.map((p) => ({ ...p, bot_id: selected })),
      wick_scalps: bot.data.wick_scalps.map((w) => ({ ...w, bot_id: selected })),
    };
  }, [merged, selected]);

  const connCount = merged?.bots.filter((b) => b.connected).length ?? 0;
  const totalBots = bots.length;
  const connected = selected === "all" ? connCount > 0 : (merged?.bots.find((b) => b.bot_id === selected)?.connected ?? false);

  return (
    <div className="app-container">
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        marginBottom: "0.5rem",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          <h1 style={{ color: "var(--heading)", fontSize: "1.3rem", fontWeight: 600 }}>
            Trade Borg
          </h1>
          {bots.length > 1 && (
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              style={{
                padding: "0.25rem 0.5rem",
                borderRadius: "var(--radius)",
                border: "1px solid var(--border)",
                background: "var(--surface)",
                color: "var(--text)",
                fontSize: "0.85rem",
                cursor: "pointer",
              }}
            >
              <option value="all">All Bots</option>
              {bots.map((b) => (
                <option key={b.bot_id} value={b.bot_id}>
                  {b.label}
                </option>
              ))}
            </select>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          {bots.length > 1 && selected === "all" && (
            <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
              {connCount}/{totalBots} bots
            </span>
          )}
          <span style={{
            width: 8, height: 8, borderRadius: "50%",
            background: connected ? "var(--green)" : "var(--red)",
            display: "inline-block",
          }} />
          <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
            {connected ? "Connected" : "Disconnected"}
          </span>
        </div>
      </div>

      <nav className="tabs">
        {TABS.map((t) => (
          <button
            key={t}
            className={`tab ${tab === t ? "active" : ""}`}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </nav>

      {tab === "Dashboard" && <Dashboard data={dashboardData} showBotColumn={selected === "all" && bots.length > 1} />}
      {tab === "Intel" && <Intel wsIntel={dashboardData?.intel ?? null} />}
      {tab === "Scanner" && <Scanner />}
      {tab === "Strategies" && <Strategies />}
      {tab === "Analytics" && <Analytics />}
      {tab === "Monitoring" && <Monitoring />}
      {tab === "Settings" && <Settings />}
    </div>
  );
}
