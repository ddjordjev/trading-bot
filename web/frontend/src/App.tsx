import { useState } from "react";
import { useWebSocket } from "./hooks/useWebSocket";
import { Dashboard } from "./pages/Dashboard";
import { Intel } from "./pages/Intel";
import { Scanner } from "./pages/Scanner";
import { Strategies } from "./pages/Strategies";
import { Analytics } from "./pages/Analytics";
import { Modules } from "./pages/Modules";
import { Summary } from "./pages/Summary";
import { Monitoring } from "./pages/Monitoring";
import { setToken } from "./api/client";

const TABS = [
  "Dashboard", "Intel", "Scanner", "Strategies", "Analytics", "Monitoring", "Modules", "Summary",
] as const;
type Tab = typeof TABS[number];

export function App() {
  const { data, connected } = useWebSocket();
  const [tab, setTab] = useState<Tab>("Dashboard");
  const [showTokenInput, setShowTokenInput] = useState(false);
  const [tokenValue, setTokenValue] = useState("");

  return (
    <div className="app-container">
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        marginBottom: "0.5rem",
      }}>
        <h1 style={{ color: "var(--heading)", fontSize: "1.3rem", fontWeight: 600 }}>
          Trading Bot
        </h1>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          <span style={{
            width: 8, height: 8, borderRadius: "50%",
            background: connected ? "var(--green)" : "var(--red)",
            display: "inline-block",
          }} />
          <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
            {connected ? "Connected" : "Disconnected"}
          </span>
          <button
            style={{ fontSize: "0.75rem", padding: "0.2rem 0.5rem" }}
            onClick={() => setShowTokenInput(!showTokenInput)}
            title="Set the dashboard authentication token. Required when DASHBOARD_TOKEN is configured in your .env file."
          >
            Auth
          </button>
        </div>
      </div>

      {showTokenInput && (
        <div style={{ display: "flex", gap: "0.5rem", marginBottom: "0.75rem" }}>
          <input
            type="password"
            placeholder="Dashboard token"
            value={tokenValue}
            onChange={(e) => setTokenValue(e.target.value)}
            style={{
              flex: 1, padding: "0.4rem 0.6rem", border: "1px solid var(--border)",
              borderRadius: "var(--radius)", background: "var(--surface)", color: "var(--text)",
              fontSize: "0.85rem",
            }}
            onKeyDown={(e) => { if (e.key === "Enter") setToken(tokenValue); }}
          />
          <button className="btn-primary" onClick={() => setToken(tokenValue)}>Save</button>
        </div>
      )}

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
      {tab === "Modules" && <Modules />}
      {tab === "Summary" && <Summary />}
    </div>
  );
}
