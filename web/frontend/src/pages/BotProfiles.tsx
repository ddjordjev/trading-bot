import { useEffect, useState } from "react";
import { get, post } from "../api/client";

interface BotProfile {
  id: string;
  display_name: string;
  description: string;
  style: string;
  strategies: string[];
  env_overrides: Record<string, string>;
  is_hub: boolean;
  enabled: boolean;
  container_status: string;
  balance: number | null;
  daily_pnl: number | null;
  wins: number;
  losses: number;
  open_positions: number;
}

const STYLE_COLORS: Record<string, string> = {
  momentum: "#3b82f6",
  meanrev: "#a855f7",
  swing: "#f59e0b",
};

const STATUS_DISPLAY: Record<string, { label: string; color: string }> = {
  running: { label: "Running", color: "var(--green)" },
  idle: { label: "Idle", color: "#6e7681" },
  winding_down: { label: "Winding Down", color: "#f59e0b" },
};

export function BotProfiles() {
  const [profiles, setProfiles] = useState<BotProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [toggling, setToggling] = useState<string | null>(null);

  const refresh = () => {
    get<BotProfile[]>("/api/bot-profiles")
      .then(setProfiles)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, []);

  const toggle = async (id: string) => {
    setToggling(id);
    try {
      await post(`/api/bot-profile/${id}/toggle`);
      await new Promise((r) => setTimeout(r, 500));
      refresh();
    } catch {
    } finally {
      setToggling(null);
    }
  };

  if (loading) return <div className="empty-state">Loading...</div>;

  return (
    <div>
      <h3 style={{ color: "var(--heading)", marginBottom: "0.25rem" }}>Bot Profiles</h3>
      <p style={{ color: "var(--text-muted)", fontSize: "0.8rem", marginBottom: "1rem" }}>
        All bots run as static containers. Toggle a profile to enable or disable
        trading — disabled bots stay alive but idle with no open positions.
      </p>
      <div className="module-grid">
        {profiles.map((p) => {
          const isToggling = toggling === p.id;
          const styleColor = STYLE_COLORS[p.style] || "var(--text-muted)";
          const overrideKeys = Object.keys(p.env_overrides);
          const st = STATUS_DISPLAY[p.container_status] || STATUS_DISPLAY.idle;

          return (
            <div
              className="module-card"
              key={p.id}
              style={{
                opacity: isToggling ? 0.6 : 1,
                transition: "opacity 0.3s",
              }}
            >
              <div className="module-header">
                <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                  <h3 style={{ margin: 0 }}>{p.display_name}</h3>
                  <span
                    style={{
                      fontSize: "0.65rem",
                      padding: "0.1rem 0.4rem",
                      borderRadius: "8px",
                      background: styleColor + "22",
                      color: styleColor,
                      fontWeight: 600,
                      textTransform: "uppercase",
                      letterSpacing: "0.5px",
                    }}
                  >
                    {p.style}
                  </span>
                </div>
                {p.is_hub ? (
                  <span
                    style={{
                      fontSize: "0.7rem",
                      color: "var(--text-muted)",
                      padding: "0.2rem 0.5rem",
                      background: "var(--surface)",
                      borderRadius: "var(--radius)",
                      border: "1px solid var(--border)",
                    }}
                  >
                    Hub
                  </span>
                ) : (
                  <button
                    className={`toggle ${p.enabled ? "on" : "off"}`}
                    onClick={() => toggle(p.id)}
                    disabled={isToggling}
                    title={
                      p.enabled
                        ? "Profile enabled. Disable to force idle."
                        : "Profile disabled. Enable to allow trading."
                    }
                  />
                )}
              </div>
              <p style={{ margin: "0.4rem 0 0.5rem", fontSize: "0.85rem" }}>{p.description}</p>
              <div style={{ fontSize: "0.8rem" }}>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    padding: "0.15rem 0",
                  }}
                >
                  <span style={{ color: "var(--text-muted)" }}>strategies</span>
                  <span style={{ color: "var(--heading)", textAlign: "right", maxWidth: "60%" }}>
                    {p.strategies.join(", ")}
                  </span>
                </div>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    padding: "0.15rem 0",
                  }}
                >
                  <span style={{ color: "var(--text-muted)" }}>status</span>
                  <span
                    style={{
                      color: isToggling ? "var(--text-muted)" : st.color,
                    }}
                  >
                    {isToggling
                      ? p.enabled
                        ? "Disabling..."
                        : "Enabling..."
                      : st.label}
                  </span>
                </div>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    padding: "0.15rem 0",
                  }}
                >
                  <span style={{ color: "var(--text-muted)" }}>profile</span>
                  <span style={{ color: p.enabled ? "var(--green)" : "#6e7681" }}>
                    {p.enabled ? "Enabled" : "Disabled"}
                  </span>
                </div>
                {overrideKeys.length > 0 && (
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      padding: "0.15rem 0",
                    }}
                  >
                    <span style={{ color: "var(--text-muted)" }}>overrides</span>
                    <span style={{ color: "var(--heading)" }}>{overrideKeys.length} params</span>
                  </div>
                )}
                {p.balance != null && (
                  <>
                    <div style={{ borderTop: "1px solid var(--border)", margin: "0.4rem 0" }} />
                    <div style={{ display: "flex", justifyContent: "space-between", padding: "0.15rem 0" }}>
                      <span style={{ color: "var(--text-muted)" }}>balance</span>
                      <span style={{ color: "var(--heading)" }}>
                        ${p.balance.toFixed(2)}
                        {p.daily_pnl != null && (
                          <span style={{ color: p.daily_pnl >= 0 ? "var(--green)" : "var(--red)", marginLeft: 6, fontSize: "0.75rem" }}>
                            {p.daily_pnl >= 0 ? "+" : ""}{p.daily_pnl.toFixed(2)}
                          </span>
                        )}
                      </span>
                    </div>
                    <div style={{ display: "flex", justifyContent: "space-between", padding: "0.15rem 0" }}>
                      <span style={{ color: "var(--text-muted)" }}>trades</span>
                      <span>
                        <span style={{ color: "var(--green)" }}>{p.wins}W</span>
                        {" / "}
                        <span style={{ color: "var(--red)" }}>{p.losses}L</span>
                        {(p.wins + p.losses) > 0 && (
                          <span style={{ color: "var(--text-muted)", marginLeft: 6, fontSize: "0.75rem" }}>
                            ({Math.round(p.wins / (p.wins + p.losses) * 100)}%)
                          </span>
                        )}
                      </span>
                    </div>
                    {p.open_positions > 0 && (
                      <div style={{ display: "flex", justifyContent: "space-between", padding: "0.15rem 0" }}>
                        <span style={{ color: "var(--text-muted)" }}>positions</span>
                        <span style={{ color: "var(--accent)" }}>{p.open_positions} open</span>
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
