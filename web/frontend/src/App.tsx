import { useState, useEffect, useCallback } from "react";
import { useWebSocket } from "./hooks/useWebSocket";
import { Dashboard } from "./pages/Dashboard";
import { Intel } from "./pages/Intel";
import { Scanner } from "./pages/Scanner";
import { Strategies } from "./pages/Strategies";
import { Analytics } from "./pages/Analytics";
import { Monitoring } from "./pages/Monitoring";
import { Settings } from "./pages/Settings";
import { setBotPort, get } from "./api/client";

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
  const [activeBot, setActiveBot] = useState<string>("");
  const { data, connected, reconnect } = useWebSocket();
  const [tab, setTab] = useState<Tab>("Dashboard");

  useEffect(() => {
    get<BotInfo[]>("/api/bots")
      .then((list) => {
        setBots(list);
        if (list.length > 0 && !activeBot) {
          setActiveBot(list[0].bot_id);
        }
      })
      .catch(() => {});
  }, []);

  const switchBot = useCallback((botId: string) => {
    const bot = bots.find((b) => b.bot_id === botId);
    if (!bot) return;
    setActiveBot(botId);
    setBotPort(bot.port);
    reconnect();
  }, [bots, reconnect]);

  const multiBot = bots.length > 1;
  const activeBotLabel = bots.find((b) => b.bot_id === activeBot)?.label || activeBot;

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
          {multiBot && (
            <select
              value={activeBot}
              onChange={(e) => switchBot(e.target.value)}
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
              {bots.map((b) => (
                <option key={b.bot_id} value={b.bot_id}>
                  {b.label}
                </option>
              ))}
            </select>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          {multiBot && (
            <span style={{ fontSize: "0.75rem", color: "var(--accent)", fontWeight: 500 }}>
              {activeBotLabel}
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

      {tab === "Dashboard" && <Dashboard data={data} />}
      {tab === "Intel" && <Intel wsIntel={data?.intel ?? null} />}
      {tab === "Scanner" && <Scanner />}
      {tab === "Strategies" && <Strategies />}
      {tab === "Analytics" && <Analytics />}
      {tab === "Monitoring" && <Monitoring />}
      {tab === "Settings" && <Settings />}
    </div>
  );
}
