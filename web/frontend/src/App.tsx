import { useState, useMemo } from "react";
import { useWebSocket } from "./hooks/useWebSocket";
import type { FullSnapshot } from "./hooks/useWebSocket";
import { Dashboard } from "./pages/Dashboard";
import { Intel } from "./pages/Intel";
import { Scanner } from "./pages/Scanner";
import { Strategies } from "./pages/Strategies";
import { Analytics } from "./pages/Analytics";
import { Monitoring } from "./pages/Monitoring";
import { Settings } from "./pages/Settings";

const FALLBACK_EXCHANGES = ["MEXC", "BINANCE", "BYBIT"];

const TABS = [
  "Dashboard", "Intel", "Scanner", "Strategies", "Analytics", "Monitoring", "Settings",
] as const;
type Tab = typeof TABS[number];

const LABEL_MAP: Record<string, string> = {
  momentum: "Momentum",
  meanrev: "Mean Reversion",
  swing: "Swing",
};

export function App() {
  const [selected, setSelected] = useState("all");
  const [exchangeFilter, setExchangeFilter] = useState("all");
  const [tab, setTab] = useState<Tab>("Dashboard");

  const { data: snapshot, connected } = useWebSocket();

  const bots = useMemo(() => {
    if (!snapshot?.bots) return [];
    return snapshot.bots.map((b) => ({
      bot_id: b.bot_id,
      label: LABEL_MAP[b.bot_id] || b.bot_id.charAt(0).toUpperCase() + b.bot_id.slice(1),
      exchange: b.exchange || "",
    }));
  }, [snapshot?.bots]);

  const exchanges = useMemo(() => {
    const fromBots = [...new Set(bots.map((b) => b.exchange.toUpperCase()).filter(Boolean))];
    return fromBots.length > 0 ? fromBots.sort() : FALLBACK_EXCHANGES;
  }, [bots]);

  const dashboardData: FullSnapshot | null = useMemo(() => {
    if (!snapshot) return null;

    const exMatch = (item: any) =>
      exchangeFilter === "all" ||
      (item.exchange_name || "").toUpperCase() === exchangeFilter;

    if (selected === "all") {
      return {
        status: snapshot.status,
        positions: snapshot.positions.filter(exMatch),
        wick_scalps: snapshot.wick_scalps.filter(exMatch),
        logs: snapshot.logs,
        intel: snapshot.intel,
        bots: snapshot.bots,
      };
    }

    const bot = snapshot.bots?.find((b) => b.bot_id === selected);
    if (!bot?.data) return null;
    const exName = bot.exchange || "";
    const taggedPositions = bot.data.positions.map((p) => ({ ...p, bot_id: selected, exchange_name: exName }));
    const taggedWicks = bot.data.wick_scalps.map((w) => ({ ...w, bot_id: selected, exchange_name: exName }));
    return {
      status: bot.data.status,
      positions: taggedPositions.filter(exMatch),
      wick_scalps: taggedWicks.filter(exMatch),
      logs: bot.data.logs || [],
      intel: snapshot.intel,
      bots: snapshot.bots,
    };
  }, [snapshot, selected, exchangeFilter]);

  return (
    <div className="app-container">
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        marginBottom: "0.5rem",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          <div style={{
            display: "flex",
            alignItems: "center",
            gap: "0.5rem",
          }}>
            <div style={{
              width: 36,
              height: 36,
              borderRadius: 8,
              backgroundImage: "url(/trade-borg-icon.png)",
              backgroundSize: "cover",
              backgroundPosition: "center",
              flexShrink: 0,
            }} />
            <h1 style={{ color: "var(--heading)", fontSize: "1.3rem", fontWeight: 600 }}>
              Trade Borg
            </h1>
          </div>
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
          <select
            value={exchangeFilter}
            onChange={(e) => setExchangeFilter(e.target.value)}
            style={{
              padding: "0.25rem 0.5rem",
              borderRadius: "var(--radius)",
              border: "1px solid var(--border)",
              background: "var(--surface)",
              color: "var(--text)",
              fontSize: "0.8rem",
              cursor: "pointer",
            }}
          >
            <option value="all">All Exchanges</option>
            {exchanges.map((ex) => (
              <option key={ex} value={ex}>{ex}</option>
            ))}
          </select>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          {bots.length > 1 && selected === "all" && (
            <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
              {bots.filter((b) => snapshot?.bots?.find((sb) => sb.bot_id === b.bot_id)?.connected).length}/{bots.length} bots
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

      {tab === "Dashboard" && <Dashboard data={dashboardData} showBotColumn={bots.length > 1} bots={bots} />}
      {tab === "Intel" && <Intel wsIntel={dashboardData?.intel ?? null} />}
      {tab === "Scanner" && <Scanner />}
      {tab === "Strategies" && <Strategies bots={[]} />}
      {tab === "Analytics" && <Analytics bots={[]} />}
      {tab === "Monitoring" && <Monitoring />}
      {tab === "Settings" && <Settings />}
    </div>
  );
}
