import type { FullSnapshot } from "../hooks/useWebSocket";
import { postBody } from "../api/client";
import { PositionRow } from "../components/PositionRow";
import { LogViewer } from "../components/LogViewer";
import { useEffect, useState } from "react";

const ALLOWED_EXCHANGES = new Set(["BINANCE", "BYBIT"]);

function buildExchangeSymbolUrl(symbol: string, exchange: string | undefined): string {
  const ex = String(exchange || "").toUpperCase();
  if (!symbol || !ex) return "";
  const clean = symbol.replace("/", "").toUpperCase();
  if (ex === "BINANCE") return `https://www.binance.com/en/futures/${clean}`;
  if (ex === "BYBIT") return `https://www.bybit.com/trade/usdt/${clean}`;
  return "";
}

interface Props {
  data: FullSnapshot | null;
  showBotColumn?: boolean;
  bots?: { bot_id: string; label: string; exchange: string }[];
  exchangeFilter?: string;
  onExchangeFilterChange?: (exchange: string) => void;
  exchanges?: string[];
}

export function Dashboard({ data, showBotColumn = false, bots = [], exchangeFilter = "all", onExchangeFilterChange, exchanges = [] }: Props) {
  const [actionMsg, setActionMsg] = useState("");
  const [logsExpanded, setLogsExpanded] = useState(true);
  const [actionTarget, setActionTarget] = useState("all");
  const [bulkAction, setBulkAction] = useState("");
  const [orphanAssignees, setOrphanAssignees] = useState<Record<string, string>>({});
  const [manualStopOverride, setManualStopOverride] = useState<boolean | null>(null);
  const hasData = !!data;

  const s = (data?.status ?? {}) as Partial<FullSnapshot["status"]>;
  const num = (v: unknown, fallback = 0) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  };
  const str = (v: unknown, fallback = "") => (typeof v === "string" ? v : fallback);
  const bool = (v: unknown, fallback = false) => (typeof v === "boolean" ? v : fallback);
  const positions = Array.isArray(data?.positions) ? data.positions : [];
  const wickScalps = Array.isArray(data?.wick_scalps) ? data.wick_scalps : [];
  const orphanPositions = Array.isArray((data as any)?.orphan_positions) ? ((data as any).orphan_positions as any[]) : [];
  const logs = Array.isArray(data?.logs) ? data.logs : [];

  const manualStopActive = manualStopOverride ?? s.manual_stop_active;
  const exchangeAccessHalted = bool((s as any).exchange_access_halted);
  const exchangeAccessExchange = str((s as any).exchange_access_alert_exchange, "Exchange");
  const exchangeAccessBanner = `${exchangeAccessExchange} API key stopped working. Please check the config manually.`;
  const pnlClass = num(s.daily_pnl_pct) >= 0 ? "pnl-positive" : "pnl-negative";

  const eb = data?.exchange_balances ?? {};
  const exchangeBalance = exchangeFilter === "all"
    ? Object.values(eb).reduce((a, b) => a + b, 0)
    : eb[exchangeFilter] ?? 0;
  const displayBalance = exchangeBalance > 0 ? exchangeBalance : num(s.balance);

  const safeExchanges = exchanges.filter((ex) => ALLOWED_EXCHANGES.has(String(ex || "").toUpperCase()));
  const visibleBots = exchangeFilter === "all"
    ? bots
    : bots.filter((b) => b.exchange.toUpperCase() === exchangeFilter);
  const visibleOrphans = exchangeFilter === "all"
    ? orphanPositions
    : orphanPositions.filter((o) => String((o as any).exchange_name || "").toUpperCase() === exchangeFilter);
  const activeExchanges = Array.from(
    new Set(
      ((data?.bots || []) as any[])
        .filter((b) => b && b.connected)
        .map((b) => String(b.exchange || "").toUpperCase())
        .filter((ex) => ALLOWED_EXCHANGES.has(ex)),
    ),
  ).sort();

  const defaultAssignee = (orphan: any): string => {
    const original = String(orphan.originally_opened_by || "").trim();
    if (original && bots.some((b) => b.bot_id === original)) return original;
    const ex = String(orphan.exchange_name || "").toUpperCase();
    const sameExchange = bots.filter((b) => b.exchange.toUpperCase() === ex);
    if (sameExchange.length > 0) return sameExchange[0].bot_id;
    return bots[0]?.bot_id || "";
  };

  const resolveTargets = (action: string): string[] | null => {
    if (actionTarget === "-") {
      const opts = visibleBots.map((b) => b.bot_id).join(", ");
      const choice = prompt(`Which bot should "${action}" target?\nOptions: ${opts}, all`);
      if (!choice) return null;
      const picked = choice.trim().toLowerCase();
      if (picked === "all") return visibleBots.map((b) => b.bot_id);
      return [picked];
    }
    if (actionTarget === "all") return visibleBots.map((b) => b.bot_id);
    return [actionTarget];
  };

  const doAction = async (label: string, fn: (botId: string) => Promise<unknown>, progressMsg?: string) => {
    const targets = resolveTargets(label);
    if (targets === null || targets.length === 0) return;
    if (progressMsg) setBulkAction(progressMsg);
    const results: string[] = [];
    let successCount = 0;

    const parseAction = (resp: unknown): { success: boolean; message?: string } => {
      if (!resp || typeof resp !== "object") return { success: true };
      const maybe = resp as { success?: unknown; message?: unknown };
      if (typeof maybe.success === "boolean") {
        return { success: maybe.success, message: typeof maybe.message === "string" ? maybe.message : undefined };
      }
      return { success: true };
    };

    const settled = await Promise.allSettled(targets.map((t) => fn(t)));
    settled.forEach((item, idx) => {
      const t = targets[idx];
      if (item.status === "fulfilled") {
        const parsed = parseAction(item.value);
        if (parsed.success) {
          successCount += 1;
          results.push(`${t}: ${parsed.message || "OK"}`);
        } else {
          results.push(`${t}: ${parsed.message || "failed"}`);
        }
      } else {
        const reason = item.reason instanceof Error ? item.reason.message : String(item.reason);
        results.push(`${t}: ${reason}`);
      }
    });
    const allSucceeded = successCount === targets.length;
    if (allSucceeded) {
      if (label === "Resume") setManualStopOverride(false);
      if (label === "Halt") setManualStopOverride(true);
    }
    setBulkAction("");
    setActionMsg(`${label} — ${results.join(", ")}`);
    setTimeout(() => setActionMsg(""), 4000);
  };

  useEffect(() => {
    if (manualStopOverride === null) return;
    if (bool(s.manual_stop_active) === manualStopOverride) {
      setManualStopOverride(null);
      return;
    }
    const timer = setTimeout(() => setManualStopOverride(null), 10000);
    return () => clearTimeout(timer);
  }, [manualStopOverride, s.manual_stop_active]);

  if (!hasData) return <div className="empty-state">Connecting...</div>;

  const fmtUptime = (sec: number) => {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  };

  return (
    <div>
      <div className="stat-grid">
        <div className="stat-card">
          <div className="label">Balance / Available</div>
          <div className="value">${Math.round(displayBalance).toLocaleString()}</div>
          <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: 2 }}>
            ${Math.round(num(s.available_margin)).toLocaleString()} available
          </div>
        </div>
        <div className="stat-card">
          <div className="label">Daily PnL</div>
          <div className={`value ${pnlClass}`}>
            {num(s.daily_pnl) >= 0 ? "+" : ""}${num(s.daily_pnl).toFixed(2)}
          </div>
          <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: 2 }}>
            {num(s.daily_pnl_pct) >= 0 ? "+" : ""}{num(s.daily_pnl_pct).toFixed(2)}%
          </div>
        </div>
        <div className="stat-card">
          <div className="label">Total Growth</div>
          <div className={`value ${num(s.total_growth_pct) >= 0 ? "pnl-positive" : "pnl-negative"}`}>
            {num(s.total_growth_usd) >= 0 ? "+" : ""}${num(s.total_growth_usd).toFixed(2)}
          </div>
          <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: 2 }}>
            {num(s.total_growth_pct) >= 0 ? "+" : ""}{num(s.total_growth_pct).toFixed(1)}%
          </div>
        </div>
        {num(s.profit_buffer_pct) > 0 && (
          <div className="stat-card">
            <div className="label">Profit Buffer</div>
            <div className="value" style={{ color: "var(--green)" }}>
              {num(s.profit_buffer_pct).toFixed(1)}%
            </div>
            <button
              style={{
                marginTop: 6, padding: "3px 10px", fontSize: "0.7rem",
                background: "var(--surface)", border: "1px solid var(--border)",
                color: "var(--text)", borderRadius: 4, cursor: "pointer",
              }}
              title="Lock in your profits: resets the house-money buffer to 0 and restores the base daily loss limit."
              onClick={() => doAction("Lock Profits", () => postBody("/api/reset-profit-buffer", {}))}
            >
              Lock Profits
            </button>
          </div>
        )}
        <div className="stat-card">
          <div className="label">Bot Status</div>
          <div className="value" style={{ color: bool(s.running) ? "var(--green)" : "var(--red)" }}>
            {bool(s.running) ? "Running" : "Stopped"}
            {manualStopActive && <span style={{ color: "var(--yellow)", fontSize: "0.7rem" }}> (HALTED)</span>}
          </div>
        </div>
        {activeExchanges.length > 0 && (
          <div className="stat-card">
            <div className="label">Exchange</div>
            <div className="value">
              {activeExchanges.length === 1 && str(s.exchange_url) ? (
                <a
                  href={str(s.exchange_url)}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: "var(--accent)", textDecoration: "none", fontSize: "0.9rem" }}
                >
                  {activeExchanges[0]} ↗
                </a>
              ) : (
                <span style={{ color: "var(--heading)", fontSize: "0.9rem" }}>
                  {activeExchanges.join(", ")}
                </span>
              )}
              <div style={{ fontSize: "0.65rem", color: "var(--text-muted)", marginTop: 2 }}>
                {str(s.trading_mode).startsWith("paper") ? str(s.trading_mode).toUpperCase().replace("_", " ") : "LIVE"}
              </div>
            </div>
          </div>
        )}
        <div className="stat-card">
          <div className="label">Uptime</div>
          <div className="value">{fmtUptime(num(s.uptime_seconds))}</div>
        </div>
      </div>

      <div className="controls">
        {onExchangeFilterChange && safeExchanges.length > 0 && (
          <select
            value={exchangeFilter}
            onChange={(e) => onExchangeFilterChange(e.target.value)}
            title="Filter bots and actions by exchange."
            style={{
              padding: "0.3rem 0.5rem",
              borderRadius: "var(--radius)",
              border: "1px solid var(--border)",
              background: "var(--surface)",
              color: "var(--text)",
              fontSize: "0.8rem",
              cursor: "pointer",
            }}
          >
            <option value="all">All Exchanges</option>
            {safeExchanges.map((ex) => (
              <option key={ex} value={ex}>{ex}</option>
            ))}
          </select>
        )}
        <select
          value={actionTarget}
          onChange={(e) => setActionTarget(e.target.value)}
          title="Choose which bot(s) to target with actions."
          style={{
            padding: "0.3rem 0.5rem",
            borderRadius: "var(--radius)",
            border: "1px solid var(--border)",
            background: "var(--surface)",
            color: "var(--text)",
            fontSize: "0.8rem",
            cursor: "pointer",
            minWidth: 100,
          }}
        >
          <option value="-">—</option>
          <option value="all">
            {exchangeFilter !== "all" ? `All ${exchangeFilter} Bots` : "All Bots"}
          </option>
          {visibleBots.map((b) => (
            <option key={b.bot_id} value={b.bot_id}>
              {b.label}{exchangeFilter === "all" && b.exchange ? ` (${b.exchange})` : ""}
            </option>
          ))}
        </select>
        {bulkAction ? (
          <span style={{ alignSelf: "center", fontSize: "0.85rem", color: "var(--yellow)", fontWeight: 600 }}>
            {bulkAction}
          </span>
        ) : (
          <>
            <button
              className="btn-warning"
              title="Temporarily halt taking new trades; bot keeps running and managing existing positions."
              onClick={() => doAction("Halt", (id) => postBody("/api/stop-trading", { bot_id: id }), "Halting...")}
            >
              Halt
            </button>
            <button
              className="btn-primary"
              title="Resume taking new trades after a halt."
              onClick={() => doAction("Resume", (id) => postBody("/api/resume-trading", { bot_id: id }), "Resuming...")}
            >
              Resume
            </button>
            <button
              className="btn-danger"
              title="Closes all current positions in the market. If you want to stop new trading from happening, click Halt first."
              onClick={() => {
                const targets = resolveTargets("Close All");
                if (!targets || targets.length === 0) return;
                const label = targets.length === 1 ? targets[0] : `${targets.length} bots`;
                if (confirm(`Close ALL positions on [${label}]?`))
                  doAction("Close All", (id) => postBody("/api/close-all", { bot_id: id }), "Closing all positions...");
              }}
            >
              Close All
            </button>
          </>
        )}
        {actionMsg && (
          <span style={{ alignSelf: "center", fontSize: "0.85rem", color: "var(--accent)" }}>
            {actionMsg}
          </span>
        )}
      </div>

      <h3 style={{ color: "var(--heading)", marginBottom: "0.5rem" }}>
        Open Positions ({positions.length})
      </h3>
      {exchangeAccessHalted && (
        <div
          style={{
            marginBottom: "0.75rem",
            padding: "0.65rem 0.85rem",
            borderRadius: 6,
            border: "2px solid #ff3b3b",
            background: "rgba(255, 59, 59, 0.16)",
            color: "#ff4d4d",
            fontWeight: 800,
            fontSize: "0.95rem",
            letterSpacing: "0.02em",
            textTransform: "uppercase",
          }}
        >
          {exchangeAccessBanner}
        </div>
      )}
      {positions.length === 0 ? (
        <div className="empty-state">No open positions</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                {showBotColumn && <th>Exchange</th>}
                {showBotColumn && <th>Bot/Strategy</th>}
                <th>Entry</th>
                <th>Current</th>
                <th>Size</th>
                <th>Margin</th>
                <th>PnL</th>
                <th>CEX SL / BOT SL</th>
                <th>Age</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <PositionRow key={`${(p as any).bot_id || ""}:${p.symbol}`} position={p} onAction={() => {}} showBot={showBotColumn} bulkAction={bulkAction} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {visibleOrphans.length > 0 && (
        <>
          <h3 style={{ color: "var(--heading)", margin: "1.5rem 0 0.5rem" }}>
            Orphan Positions ({visibleOrphans.length})
          </h3>
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Exchange</th>
                <th>Side</th>
                <th>Amount</th>
                <th>Entry</th>
                <th>Current</th>
                <th>Detected By</th>
                <th>Originally Opened By</th>
                <th>Reason</th>
                <th>Recommended Bot</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {visibleOrphans.map((o) => {
                const key = `${String(o.exchange_name || "")}:${o.symbol}`;
                const suggested = defaultAssignee(o);
                const chosenBot = orphanAssignees[key] || suggested;
                const originalOwner = String(o.originally_opened_by || "").trim();
                const ownerRunning = o.owner_running === true;
                const ownerOptionMissing = !!originalOwner && !bots.some((b) => b.bot_id === originalOwner);
                const reason = String(o.orphan_reason || "").trim() || "unknown";
                return (
                  <tr key={key}>
                    <td>
                      {buildExchangeSymbolUrl(String(o.symbol || ""), String(o.exchange_name || "")) ? (
                        <a
                          href={buildExchangeSymbolUrl(String(o.symbol || ""), String(o.exchange_name || ""))}
                          target="_blank"
                          rel="noreferrer"
                          style={{ color: "var(--blue)" }}
                        >
                          {o.symbol} ↗
                        </a>
                      ) : (
                        <strong>{o.symbol}</strong>
                      )}
                    </td>
                    <td style={{ fontSize: "0.75rem", fontWeight: 500, textTransform: "uppercase", color: "var(--text-muted)" }}>{String(o.exchange_name || "—")}</td>
                    <td style={{ color: String(o.side || "").toLowerCase() === "long" ? "var(--green)" : "var(--red)" }}>{String(o.side || "").toUpperCase()}</td>
                    <td>{Number(o.amount || 0).toFixed(6)}</td>
                    <td>{Number(o.entry_price || 0).toFixed(Number(o.entry_price || 0) < 1 ? 6 : 2)}</td>
                    <td>{Number(o.current_price || 0).toFixed(Number(o.current_price || 0) < 1 ? 6 : 2)}</td>
                    <td style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>{String(o.detected_by_bot || "—")}</td>
                    <td style={{ fontSize: "0.75rem", color: ownerRunning ? "var(--text-muted)" : "var(--yellow)" }}>
                      {originalOwner || "—"}
                    </td>
                    <td style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>{reason}</td>
                    <td>
                      <select
                        value={chosenBot}
                        onChange={(e) => setOrphanAssignees((prev) => ({ ...prev, [key]: e.target.value }))}
                        style={{ minWidth: 120 }}
                      >
                        {ownerOptionMissing && (
                          <option key={originalOwner} value={originalOwner} disabled>
                            {originalOwner} (offline)
                          </option>
                        )}
                        {bots.map((b) => (
                          <option key={b.bot_id} value={b.bot_id}>
                            {b.label} ({b.exchange})
                          </option>
                        ))}
                      </select>
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <button
                        className="btn-primary"
                        style={{ marginRight: 6 }}
                        disabled={!chosenBot}
                        title="Assign orphan to selected bot"
                        onClick={async () => {
                          const res = await postBody("/api/orphan/assign", {
                            symbol: o.symbol,
                            bot_id: chosenBot,
                            strategy: "manual_claim",
                          });
                          const ok = !!(res && typeof res === "object" && (res as any).success);
                          setActionMsg(`Assign ${o.symbol}: ${ok ? "OK" : ((res as any)?.message || "failed")}`);
                          setTimeout(() => setActionMsg(""), 4000);
                        }}
                      >
                        Assign
                      </button>
                      <button
                        className="btn-danger"
                        disabled={!chosenBot}
                        onClick={async () => {
                          if (!confirm(`Close orphan ${o.symbol} now via ${chosenBot}?`)) return;
                          const res = await postBody("/api/orphan/close", { symbol: o.symbol, bot_id: chosenBot });
                          const ok = !!(res && typeof res === "object" && (res as any).success);
                          setActionMsg(`Close ${o.symbol}: ${ok ? "OK" : ((res as any)?.message || "failed")}`);
                          setTimeout(() => setActionMsg(""), 4000);
                        }}
                      >
                        Close now
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        </>
      )}

      {wickScalps.length > 0 && (
        <>
          <h3 style={{ color: "var(--heading)", margin: "1.5rem 0 0.5rem" }}>
            Active Wick Scalps ({wickScalps.length})
          </h3>
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                {showBotColumn && <th>Exchange</th>}
                {showBotColumn && <th>Bot</th>}
                <th>Side</th>
                <th>Entry</th>
                <th>Amount</th>
                <th>Age</th>
                <th>Max Hold</th>
              </tr>
            </thead>
            <tbody>
              {wickScalps.map((ws) => (
                <tr key={`${(ws as any).bot_id || ""}:${ws.symbol}`}>
                  <td>{ws.symbol}</td>
                  {showBotColumn && <td style={{ fontSize: "0.75rem", fontWeight: 500, textTransform: "uppercase", color: "var(--text-muted)" }}>{(ws as any).exchange_name || "—"}</td>}
                  {showBotColumn && <td style={{ fontSize: "0.75rem", fontWeight: 600, textTransform: "uppercase", color: "var(--text-muted)" }}>{(ws as any).bot_id || "—"}</td>}
                  <td style={{ color: ws.scalp_side.toLowerCase() === "long" ? "var(--green)" : ws.scalp_side.toLowerCase() === "short" ? "var(--red)" : "var(--text)" }}>
                    {ws.scalp_side.toUpperCase()}
                  </td>
                  <td>{ws.entry_price.toFixed(ws.entry_price < 1 ? 6 : 2)}</td>
                  <td>{ws.amount.toFixed(6)}</td>
                  <td>{ws.age_minutes.toFixed(0)}m</td>
                  <td>{ws.max_hold_minutes}m</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <div
        style={{ display: "flex", alignItems: "center", gap: "0.5rem", margin: "1.5rem 0 0.5rem", cursor: "pointer" }}
        onClick={() => setLogsExpanded(!logsExpanded)}
      >
        <span style={{ fontSize: "1.4rem", lineHeight: 1, color: "var(--text-muted)", transition: "transform 0.2s", transform: logsExpanded ? "rotate(90deg)" : "rotate(0deg)" }}>
          ▶
        </span>
        <h3 style={{ color: "var(--heading)", margin: 0 }}>
          Live Logs
        </h3>
      </div>
      {logsExpanded && <LogViewer logs={logs} />}
    </div>
  );
}
