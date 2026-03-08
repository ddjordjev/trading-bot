import type { FullSnapshot } from "../hooks/useWebSocket";
import { get, postBody } from "../api/client";
import { PositionRow } from "../components/PositionRow";
import { LogViewer } from "../components/LogViewer";
import { useEffect, useMemo, useRef, useState } from "react";
import { getBotLabel } from "../utils/botNames";
import { buildExchangeSymbolUrl } from "../utils/exchangeLinks";

const ALLOWED_EXCHANGES = new Set(["BINANCE", "BYBIT"]);

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
  const [manualSwingBusy, setManualSwingBusy] = useState(false);
  const [manualSwingMsg, setManualSwingMsg] = useState("");
  const [manualSwingExchange, setManualSwingExchange] = useState("BINANCE");
  const [manualSwingSymbol, setManualSwingSymbol] = useState("BTC/USDT");
  const [manualSwingPairs, setManualSwingPairs] = useState<string[]>([]);
  const [manualSwingPairQuery, setManualSwingPairQuery] = useState("BTC/USDT");
  const [manualSwingPairOpen, setManualSwingPairOpen] = useState(false);
  const [manualSwingCurrentPrice, setManualSwingCurrentPrice] = useState<number | null>(null);
  const [manualSwingPriceLoading, setManualSwingPriceLoading] = useState(false);
  const [manualSwingPriceError, setManualSwingPriceError] = useState("");
  const [manualSwingDirection, setManualSwingDirection] = useState("long");
  const [manualSwingFirstEntry, setManualSwingFirstEntry] = useState("");
  const [manualSwingLastEntry, setManualSwingLastEntry] = useState("");
  const [manualSwingGridCount, setManualSwingGridCount] = useState("5");
  const [manualSwingLeverage, setManualSwingLeverage] = useState("5x");
  const [manualSwingMargin, setManualSwingMargin] = useState("30");
  const [manualSwingMaxCex, setManualSwingMaxCex] = useState("3");
  const [actionTarget, setActionTarget] = useState("all");
  const [bulkAction, setBulkAction] = useState("");
  const [orphanAssignees, setOrphanAssignees] = useState<Record<string, string>>({});
  const [manualStopOverride, setManualStopOverride] = useState<boolean | null>(null);
  const hasData = !!data;
  const manualSwingPairBoxRef = useRef<HTMLDivElement | null>(null);

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
  const manualSwingExchanges = safeExchanges.length > 0 ? safeExchanges : ["BINANCE"];

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await get<{ status?: string; pairs?: string[] }>(
          `/api/manual-swing/pairs?exchange=${encodeURIComponent(manualSwingExchange)}`,
        );
        if (!alive) return;
        const pairs = Array.isArray(res?.pairs) ? res.pairs.filter((p): p is string => typeof p === "string" && p.trim().length > 0) : [];
        setManualSwingPairs(pairs);
        if (pairs.length > 0) {
          setManualSwingSymbol((prev) => {
            const next = pairs.includes(prev) ? prev : pairs[0];
            setManualSwingPairQuery(next);
            return next;
          });
        } else {
          setManualSwingSymbol("");
          setManualSwingPairQuery("");
        }
      } catch {
        if (!alive) return;
        setManualSwingPairs([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, [manualSwingExchange]);

  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      if (!manualSwingPairBoxRef.current) return;
      if (!manualSwingPairBoxRef.current.contains(e.target as Node)) {
        setManualSwingPairOpen(false);
      }
    };
    document.addEventListener("mousedown", onMouseDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    if (!manualSwingSymbol) {
      setManualSwingCurrentPrice(null);
      setManualSwingPriceLoading(false);
      setManualSwingPriceError("");
      return () => {
        alive = false;
      };
    }
    setManualSwingPriceLoading(true);
    setManualSwingPriceError("");
    void (async () => {
      try {
        const res = await get<{ status?: string; price?: number }>(
          `/api/manual-swing/pair-price?exchange=${encodeURIComponent(manualSwingExchange)}&symbol=${encodeURIComponent(manualSwingSymbol)}`,
        );
        if (!alive) return;
        const raw = Number(res?.price);
        if (res?.status === "ok" && Number.isFinite(raw) && raw > 0) {
          setManualSwingCurrentPrice(raw);
          setManualSwingFirstEntry(String(raw));
          setManualSwingLastEntry("");
          return;
        }
        setManualSwingCurrentPrice(null);
        setManualSwingPriceError("Price unavailable");
      } catch {
        if (!alive) return;
        setManualSwingCurrentPrice(null);
        setManualSwingPriceError("Price unavailable");
      } finally {
        if (alive) setManualSwingPriceLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [manualSwingExchange, manualSwingSymbol]);

  const manualSwingFilteredPairs = useMemo(() => {
    const query = manualSwingPairQuery.trim().toUpperCase();
    if (!query) return manualSwingPairs.slice(0, 120);
    return manualSwingPairs
      .filter((pair) => pair.toUpperCase().includes(query))
      .slice(0, 120);
  }, [manualSwingPairs, manualSwingPairQuery]);

  const mergedPositions = useMemo(() => {
    const wickAsPositions = wickScalps.map((ws) => {
      const entry = Number(ws.entry_price || 0);
      const amount = Number(ws.amount || 0);
      const sideRaw = String(ws.scalp_side || "").toLowerCase();
      const side = sideRaw === "long" ? "buy" : sideRaw === "short" ? "sell" : String(ws.scalp_side || "");
      return {
        symbol: String(ws.symbol || ""),
        side,
        amount,
        entry_price: entry,
        current_price: entry,
        pnl_pct: 0,
        pnl_usd: 0,
        leverage: 1,
        market_type: "futures",
        strategy: "wick_scalp",
        stop_loss: null,
        take_profit: null,
        exchange_stop_loss: null,
        exchange_take_profit: null,
        bot_stop_loss: null,
        bot_take_profit: null,
        effective_stop_loss: null,
        effective_take_profit: null,
        stop_source: "none",
        tp_source: "none",
        risk_state: "none",
        close_pending: false,
        close_reason_pending: "",
        notional_value: entry * amount,
        age_minutes: Number(ws.age_minutes || 0),
        breakeven_locked: false,
        scale_mode: "",
        scale_phase: "",
        dca_count: 0,
        trade_url: "",
        is_wick_scalp: true,
        bot_id: (ws as any).bot_id,
        exchange_name: (ws as any).exchange_name,
      };
    });
    return [...positions, ...wickAsPositions];
  }, [positions, wickScalps]);

  const parseLeverageInput = (raw: string): number => {
    const normalized = String(raw || "").trim().toLowerCase().replace(/x/g, "");
    const parsed = Number(normalized);
    if (!Number.isFinite(parsed) || parsed <= 0) return 1;
    return Math.max(1, Math.round(parsed));
  };

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
      const opts = visibleBots.map((b) => `${getBotLabel(b.bot_id)} (${b.bot_id})`).join(", ");
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
        Open Positions ({mergedPositions.length})
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
      {mergedPositions.length === 0 ? (
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
              {mergedPositions.map((p) => (
                <PositionRow
                  key={`${(p as any).bot_id || ""}:${p.symbol}:${(p as any).is_wick_scalp ? "wick" : "main"}`}
                  position={p}
                  onAction={() => {}}
                  showBot={showBotColumn}
                  bulkAction={bulkAction}
                />
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
                const orphanTradeUrl = String((o as any).trade_url || "");
                const originalOwner = String(o.originally_opened_by || "").trim();
                const ownerRunning = o.owner_running === true;
                const ownerOptionMissing = !!originalOwner && !bots.some((b) => b.bot_id === originalOwner);
                const reason = String(o.orphan_reason || "").trim() || "unknown";
                return (
                  <tr key={key}>
                    <td>
                      {(orphanTradeUrl || buildExchangeSymbolUrl(String(o.symbol || ""), String(o.exchange_name || ""), str(s.trading_mode))) ? (
                        <a
                          href={orphanTradeUrl || buildExchangeSymbolUrl(String(o.symbol || ""), String(o.exchange_name || ""), str(s.trading_mode))}
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
                    <td style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                      {String(o.detected_by_bot || "").trim() ? getBotLabel(String(o.detected_by_bot || "")) : "—"}
                    </td>
                    <td style={{ fontSize: "0.75rem", color: ownerRunning ? "var(--text-muted)" : "var(--yellow)" }}>
                      {originalOwner ? getBotLabel(originalOwner) : "—"}
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
                        className="btn-secondary"
                        style={{ marginRight: 6 }}
                        disabled={!String(o.symbol || "").trim()}
                        title="Recover orphan ownership now"
                        onClick={async () => {
                          const payload: Record<string, unknown> = {
                            symbol: o.symbol,
                            exchange: String(o.exchange_name || ""),
                            strategy: "recovered_owner_claim",
                          };
                          if (chosenBot) payload.bot_id = chosenBot;
                          const res = await postBody("/api/orphan/recover-now", payload);
                          const ok = !!(res && typeof res === "object" && (res as any).success);
                          setActionMsg(`Recover ${o.symbol}: ${ok ? "OK" : ((res as any)?.message || "failed")}`);
                          setTimeout(() => setActionMsg(""), 4000);
                        }}
                      >
                        Recover now
                      </button>
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

      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", margin: "1.5rem 0 0.5rem" }}>
        <span style={{ fontSize: "1.4rem", lineHeight: 1, color: "var(--text-muted)" }}>
          ▼
        </span>
        <h3 style={{ color: "var(--heading)", margin: 0 }}>
          Manual Swing Plan
        </h3>
      </div>
      <div style={{ border: "1px solid var(--border)", borderRadius: 6, padding: "0.8rem", marginBottom: "1rem" }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: "0.5rem", marginBottom: "0.3rem" }}>
            <span style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 600 }}>Exchange</span>
            <span style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 600 }}>Pair</span>
            <span style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 600 }}>Direction</span>
            <span style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 600 }}>First Entry Price</span>
            <span style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 600 }}>Last Entry Price</span>
            <span style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 600 }}>Grid Count</span>
            <span style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 600 }}>Leverage</span>
            <span style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 600 }}>Margin Amount</span>
            <span style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 600 }}>Max Limits on CEX</span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: "0.5rem" }}>
            <select value={manualSwingExchange} onChange={(e) => setManualSwingExchange(e.target.value)}>
              {manualSwingExchanges.map((ex) => (
                <option key={ex} value={ex}>{ex}</option>
              ))}
            </select>
            <div ref={manualSwingPairBoxRef} style={{ position: "relative" }}>
              <input
                value={manualSwingPairQuery}
                onChange={(e) => {
                  setManualSwingPairQuery(e.target.value.toUpperCase());
                  setManualSwingPairOpen(true);
                }}
                onFocus={() => setManualSwingPairOpen(true)}
                placeholder={manualSwingPairs.length === 0 ? "No pairs available" : "Search pair"}
                disabled={manualSwingPairs.length === 0}
              />
              {manualSwingPairOpen && manualSwingPairs.length > 0 && (
                <div
                  style={{
                    position: "absolute",
                    top: "calc(100% + 4px)",
                    left: 0,
                    right: 0,
                    maxHeight: 280,
                    overflowY: "auto",
                    background: "var(--surface)",
                    border: "1px solid var(--border)",
                    borderRadius: 6,
                    zIndex: 50,
                    boxShadow: "0 8px 20px rgba(0, 0, 0, 0.35)",
                  }}
                >
                  {manualSwingFilteredPairs.length === 0 ? (
                    <div style={{ padding: "0.5rem 0.6rem", color: "var(--text-muted)", fontSize: "0.8rem" }}>
                      No matches
                    </div>
                  ) : (
                    manualSwingFilteredPairs.map((pair) => (
                      <button
                        key={pair}
                        type="button"
                        onClick={() => {
                          setManualSwingSymbol(pair);
                          setManualSwingPairQuery(pair);
                          setManualSwingPairOpen(false);
                        }}
                        style={{
                          display: "block",
                          width: "100%",
                          textAlign: "left",
                          padding: "0.45rem 0.6rem",
                          border: "none",
                          background: pair === manualSwingSymbol ? "var(--surface-hover)" : "transparent",
                          color: "var(--text)",
                          cursor: "pointer",
                          fontSize: "0.88rem",
                        }}
                      >
                        {pair}
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
            <select value={manualSwingDirection} onChange={(e) => setManualSwingDirection(e.target.value)}>
              <option value="long">Long</option>
              <option value="short">Short</option>
            </select>
            <input value={manualSwingFirstEntry} onChange={(e) => setManualSwingFirstEntry(e.target.value)} placeholder="First entry price" />
            <input value={manualSwingLastEntry} onChange={(e) => setManualSwingLastEntry(e.target.value)} placeholder="Last entry price" />
            <input value={manualSwingGridCount} onChange={(e) => setManualSwingGridCount(e.target.value)} placeholder="Grid count" />
            <input
              value={manualSwingLeverage}
              onChange={(e) => {
                const raw = e.target.value;
                const clean = raw.replace(/[^0-9xX]/g, "");
                if (!clean) {
                  setManualSwingLeverage("");
                  return;
                }
                const noX = clean.replace(/x/gi, "");
                setManualSwingLeverage(`${noX}x`);
              }}
              placeholder="5x"
            />
            <input value={manualSwingMargin} onChange={(e) => setManualSwingMargin(e.target.value)} placeholder="Margin amount" />
            <input value={manualSwingMaxCex} onChange={(e) => setManualSwingMaxCex(e.target.value)} placeholder="Max limits on CEX (optional)" />
          </div>
          <div style={{ marginTop: "0.45rem", fontSize: "0.75rem", color: "var(--text-muted)" }}>
            {manualSwingPriceLoading
              ? "Current CEX price: loading..."
              : manualSwingCurrentPrice !== null
                ? `Current CEX price: ${manualSwingCurrentPrice}`
                : `Current CEX price: ${manualSwingPriceError || "unavailable"}`}
          </div>
          <div style={{ marginTop: "0.6rem", display: "flex", alignItems: "center", gap: "0.5rem" }}>
            <button
              className="btn-primary"
              disabled={manualSwingBusy || !manualSwingSymbol}
              onClick={async () => {
                setManualSwingBusy(true);
                setManualSwingMsg("");
                try {
                  const payload: Record<string, unknown> = {
                    bot_id: "swing",
                    exchange: manualSwingExchange,
                    symbol: manualSwingSymbol,
                    direction: manualSwingDirection,
                    first_entry_price: Number(manualSwingFirstEntry),
                    last_entry_price: Number(manualSwingLastEntry),
                    grid_count: Number(manualSwingGridCount),
                    leverage: parseLeverageInput(manualSwingLeverage),
                    margin_amount: Number(manualSwingMargin),
                  };
                  if (manualSwingMaxCex.trim()) {
                    payload.max_concurrent_limit_orders_on_cex = Number(manualSwingMaxCex);
                  }
                  const res = await postBody("/api/manual-swing/plans", payload);
                  const status = String((res as any)?.status || "");
                  if (status === "ok") {
                    setManualSwingMsg(`Created plan ${String((res as any)?.plan?.plan_id || "")}`);
                  } else {
                    setManualSwingMsg(`Failed: ${String((res as any)?.detail || "unknown")}`);
                  }
                } catch (e: any) {
                  setManualSwingMsg(`Failed: ${e?.message || "request failed"}`);
                } finally {
                  setManualSwingBusy(false);
                }
              }}
            >
              {manualSwingBusy ? "Creating..." : "Create Manual Grid Plan"}
            </button>
            {manualSwingMsg && <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>{manualSwingMsg}</span>}
          </div>
      </div>

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
