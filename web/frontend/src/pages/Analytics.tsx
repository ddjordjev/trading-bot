import { useEffect, useState } from "react";
import { get, post, postBody } from "../api/client";

interface StrategyScore {
  strategy: string;
  total_trades: number;
  winners: number;
  losers: number;
  win_rate: number;
  avg_win_pct: number;
  avg_loss_pct: number;
  total_pnl: number;
  profit_factor: number;
  expectancy: number;
  weight: number;
  streak_current: number;
  streak_max_loss: number;
  avg_hold_minutes: number;
  best_hour_utc: number;
  worst_hour_utc: number;
  best_regime: string;
  worst_regime: string;
}

interface PatternInsight {
  pattern_type: string;
  description: string;
  severity: string;
  affected_strategy: string;
  affected_symbol: string;
  sample_size: number;
  confidence: number;
  suggestion: string;
}

interface ModSuggestion {
  id: number;
  source: string;
  status: string;
  strategy: string;
  symbol: string;
  suggestion_type: string;
  title: string;
  description: string;
  confidence: number;
  current_value: string;
  suggested_value: string;
  expected_improvement: string;
  based_on_trades: number;
  notes: string;
  updated_at: string;
}

interface LivePosition {
  symbol: string;
  side: string;
  strategy: string;
  entry_price: number;
  current_price: number;
  pnl_pct: number;
  pnl_usd: number;
  notional: number;
  leverage: number;
  age_minutes: number;
  dca_count: number;
}

interface ClosedTrade {
  id: number;
  symbol: string;
  side: string;
  strategy: string;
  scale_mode: string;
  entry_price: number;
  exit_price: number;
  amount: number;
  leverage: number;
  pnl_usd: number;
  pnl_pct: number;
  is_winner: boolean;
  hold_minutes: number;
  dca_count: number;
  opened_at: string;
  closed_at: string;
}

interface AnalyticsData {
  strategy_scores: StrategyScore[];
  patterns: PatternInsight[];
  suggestions: ModSuggestion[];
  total_trades_logged: number;
  hourly_performance: Array<{ hour_utc: number; trades: number; wins: number; avg_pnl: number; total_pnl: number }>;
  regime_performance: Array<{ market_regime: string; trades: number; wins: number; avg_pnl: number; total_pnl: number }>;
  live_positions: LivePosition[];
}

interface DailyReport {
  compound_report: string;
  history: Array<{
    day: number; date: string; start_balance: number; end_balance: number;
    pnl: number; pnl_pct: number; target_hit: boolean; trades: number;
  }>;
  winning_days: number; losing_days: number; target_hit_days: number;
  avg_daily_pnl_pct: number;
  best_day: { day: number; pnl_pct: number } | null;
  worst_day: { day: number; pnl_pct: number } | null;
  projected: { "1_week": number; "1_month": number; "3_months": number };
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

const SEVERITY_COLORS: Record<string, string> = {
  info: "var(--accent)",
  warning: "var(--yellow)",
  critical: "var(--red)",
};

const SUGGESTION_COLORS: Record<string, string> = {
  disable: "var(--red)",
  reduce_weight: "var(--yellow)",
  time_filter: "var(--accent)",
  regime_filter: "var(--purple)",
  change_param: "var(--accent)",
  process: "var(--purple)",
};

function weightColor(w: number): string {
  if (w >= 1.2) return "var(--green)";
  if (w >= 0.8) return "var(--text)";
  if (w >= 0.5) return "var(--yellow)";
  return "var(--red)";
}

function DbExplorer() {
  const [tables, setTables] = useState<DbTable[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tableData, setTableData] = useState<DbTableData | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [selectedCell, setSelectedCell] = useState<{ column: string; value: string } | null>(null);
  const [copied, setCopied] = useState(false);

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
    setSelectedCell(null);
    setCopied(false);
  };

  const copySelectedCell = async () => {
    if (!selectedCell) return;
    try {
      await navigator.clipboard.writeText(selectedCell.value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div style={{ marginTop: "0.5rem" }}>
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
                    {tableData.columns.map(col => {
                      const cellValue = row[col] == null ? "NULL" : String(row[col]);
                      return (
                        <td
                          key={col}
                          title={cellValue}
                          onClick={() => {
                            setSelectedCell({ column: col, value: cellValue });
                            setCopied(false);
                          }}
                          style={{
                            padding: "0.4rem 0.6rem", whiteSpace: "nowrap",
                            maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis",
                            cursor: "pointer",
                          }}
                        >
                          {row[col] == null ? <span style={{ opacity: 0.3 }}>NULL</span> : cellValue}
                        </td>
                      );
                    })}
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

          {selectedCell && (
            <div style={{
              borderTop: "1px solid var(--border, #30363d)",
              padding: "0.65rem 0.75rem",
              display: "flex",
              flexDirection: "column",
              gap: "0.45rem",
            }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "0.75rem" }}>
                <span style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
                  Full value: <code>{selectedCell.column}</code>
                </span>
                <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                  {copied && <span style={{ fontSize: "0.72rem", color: "var(--green)" }}>Copied</span>}
                  <button
                    type="button"
                    onClick={copySelectedCell}
                    style={{
                      padding: "0.2rem 0.5rem",
                      fontSize: "0.72rem",
                      cursor: "pointer",
                      background: "none",
                      border: "1px solid var(--border)",
                      borderRadius: 4,
                      color: "var(--text-muted)",
                    }}
                  >
                    Copy
                  </button>
                </div>
              </div>
              <textarea
                readOnly
                value={selectedCell.value}
                style={{
                  width: "100%",
                  minHeight: 82,
                  resize: "vertical",
                  border: "1px solid var(--border, #30363d)",
                  borderRadius: 6,
                  background: "var(--bg, #0d1117)",
                  color: "var(--text, #c9d1d9)",
                  padding: "0.45rem 0.55rem",
                  fontSize: "0.75rem",
                  lineHeight: 1.45,
                }}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function Analytics() {
  const [analytics, setAnalytics] = useState<AnalyticsData | null>(null);
  const [report, setReport] = useState<DailyReport | null>(null);
  const [closedTrades, setClosedTrades] = useState<ClosedTrade[]>([]);
  const [loading, setLoading] = useState(true);
  const [busySuggestionId, setBusySuggestionId] = useState<number | null>(null);
  const [tab, setTab] = useState<"live" | "closed" | "scores" | "patterns" | "suggestions" | "hourly" | "db" | "report">("live");

  const refresh = async (silent = false) => {
    if (!silent) setLoading(true);
    const [aRes, rRes, ctRes] = await Promise.allSettled([
      get<AnalyticsData>("/api/analytics"),
      get<DailyReport>("/api/daily-report"),
      get<ClosedTrade[]>("/api/closed-trades?limit=200"),
    ]);
    if (aRes.status === "fulfilled") setAnalytics(aRes.value);
    if (rRes.status === "fulfilled") setReport(rRes.value);
    if (ctRes.status === "fulfilled") setClosedTrades(ctRes.value);
    if (!silent) setLoading(false);
  };

  const triggerRefresh = async () => {
    await post("/api/analytics/refresh");
    await refresh();
  };

  const triggerOpenClawDailyReview = async () => {
    await post("/api/openclaw/daily-review/trigger");
    await refresh();
  };

  const updateSuggestionStatus = async (id: number, status: string) => {
    setBusySuggestionId(id);
    try {
      await postBody(`/api/openclaw-suggestions/${id}/status`, { status });
      await refresh(true);
    } finally {
      setBusySuggestionId(null);
    }
  };

  useEffect(() => {
    const loadingGuard = window.setTimeout(() => setLoading(false), 10000);
    void refresh(false).finally(() => window.clearTimeout(loadingGuard));
    const iv = setInterval(() => {
      // Keep DB explorer stable while user inspects rows.
      if (tab !== "db") {
        refresh(true);
      }
    }, 10000);
    return () => {
      window.clearTimeout(loadingGuard);
      clearInterval(iv);
    };
  }, [tab]);

  if (loading && !analytics) return <div className="empty-state">Loading analytics...</div>;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1rem" }}>
        <div className="stat-grid" style={{ flex: 1 }}>
          <div className="stat-card">
            <div className="label">Trades Logged</div>
            <div className="value">{analytics?.total_trades_logged ?? 0}</div>
          </div>
          <div className="stat-card">
            <div className="label">Patterns Found</div>
            <div className="value" style={{ color: (analytics?.patterns.length ?? 0) > 0 ? "var(--yellow)" : "var(--green)" }}>
              {analytics?.patterns.length ?? 0}
            </div>
          </div>
          <div className="stat-card">
            <div className="label">Suggestions</div>
            <div className="value" style={{ color: (analytics?.suggestions.length ?? 0) > 0 ? "var(--yellow)" : "var(--green)" }}>
              {analytics?.suggestions.length ?? 0}
            </div>
          </div>
          {report && (
            <>
              <div className="stat-card">
                <div className="label">Win Days / Loss Days</div>
                <div className="value">{report.winning_days} / {report.losing_days}</div>
              </div>
              <div className="stat-card">
                <div className="label">Avg Daily PnL</div>
                <div className={`value ${report.avg_daily_pnl_pct >= 0 ? "pnl-positive" : "pnl-negative"}`}>
                  {report.avg_daily_pnl_pct >= 0 ? "+" : ""}{report.avg_daily_pnl_pct.toFixed(2)}%
                </div>
              </div>
            </>
          )}
        </div>
        <div style={{ display: "flex", gap: "0.5rem", marginLeft: "1rem" }}>
          <button className="btn-primary" onClick={triggerRefresh}>
            Refresh Analytics
          </button>
          <button className="btn-secondary" onClick={triggerOpenClawDailyReview}>
            Run OC Daily Review
          </button>
        </div>
      </div>

      <div className="tabs" style={{ marginBottom: "1rem" }}>
        {(["live", "closed", "scores", "patterns", "suggestions", "hourly", "db", "report"] as const).map((t) => (
          <button key={t} className={`tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
            {t === "live" ? `Live (${analytics?.live_positions.length ?? 0})`
              : t === "closed" ? `Closed Trades (${closedTrades.length})`
              : t === "scores" ? `Strategy Stats (${analytics?.strategy_scores.length ?? 0})`
              : t === "patterns" ? `Patterns (${analytics?.patterns.length ?? 0})`
              : t === "suggestions" ? `Suggestions (${analytics?.suggestions.length ?? 0})`
              : t === "hourly" ? "Time & Regime"
              : t === "db" ? "Database Explorer"
              : "Daily Report"}
          </button>
        ))}
      </div>

      {tab === "live" && <LivePositionsTab positions={analytics?.live_positions ?? []} />}
      {tab === "closed" && <ClosedTradesTab trades={closedTrades} />}
      {tab === "scores" && <StrategyScoresTab scores={analytics?.strategy_scores ?? []} />}
      {tab === "patterns" && <PatternsTab patterns={analytics?.patterns ?? []} />}
      {tab === "suggestions" && (
        <SuggestionsTab
          suggestions={analytics?.suggestions ?? []}
          onStatusUpdate={updateSuggestionStatus}
          busySuggestionId={busySuggestionId}
        />
      )}
      {tab === "hourly" && <HourlyTab hourly={analytics?.hourly_performance ?? []} regime={analytics?.regime_performance ?? []} />}
      {tab === "db" && <DbExplorer />}
      {tab === "report" && <ReportTab report={report} />}
    </div>
  );
}

function LivePositionsTab({ positions }: { positions: LivePosition[] }) {
  if (positions.length === 0) return <div className="empty-state">No open positions right now</div>;

  const totalPnl = positions.reduce((sum, p) => sum + p.pnl_usd, 0);
  const totalNotional = positions.reduce((sum, p) => sum + p.notional, 0);

  return (
    <div>
      <div className="stat-grid" style={{ marginBottom: "1rem" }}>
        <div className="stat-card">
          <div className="label">Open Positions</div>
          <div className="value">{positions.length}</div>
        </div>
        <div className="stat-card">
          <div className="label">Total Notional</div>
          <div className="value">${Math.round(totalNotional).toLocaleString()}</div>
        </div>
        <div className="stat-card">
          <div className="label">Unrealized PnL</div>
          <div className={`value ${totalPnl >= 0 ? "pnl-positive" : "pnl-negative"}`}>
            ${totalPnl >= 0 ? "+" : ""}{totalPnl.toFixed(2)}
          </div>
        </div>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Strategy</th>
              <th>Entry</th>
              <th>Current</th>
              <th>PnL</th>
              <th>Notional</th>
              <th>Leverage</th>
              <th>DCAs</th>
              <th>Age</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.symbol}>
                <td><strong>{p.symbol}</strong></td>
                <td style={{ color: p.side === "long" || p.side === "buy" ? "var(--green)" : "var(--red)" }}>
                  {p.side.toUpperCase()}
                </td>
                <td style={{ fontSize: "0.85rem" }}>{p.strategy}</td>
                <td>{p.entry_price < 1 ? p.entry_price.toFixed(6) : p.entry_price.toFixed(2)}</td>
                <td>{p.current_price < 1 ? p.current_price.toFixed(6) : p.current_price.toFixed(2)}</td>
                <td className={p.pnl_pct >= 0 ? "pnl-positive" : "pnl-negative"} style={{ fontWeight: 600 }}>
                  {p.pnl_pct >= 0 ? "+" : ""}{p.pnl_pct.toFixed(2)}%
                  <br />
                  <span style={{ fontSize: "0.8rem" }}>${p.pnl_usd >= 0 ? "+" : ""}{p.pnl_usd.toFixed(2)}</span>
                </td>
                <td>${Math.round(p.notional).toLocaleString()}</td>
                <td>{p.leverage}x</td>
                <td>{p.dca_count}</td>
                <td>{p.age_minutes < 60 ? `${p.age_minutes.toFixed(0)}m` : `${(p.age_minutes / 60).toFixed(1)}h`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

type SortKey = "symbol" | "side" | "strategy" | "entry_price" | "exit_price" | "pnl_usd" | "leverage" | "scale_mode" | "dca_count" | "hold_minutes" | "closed_at";
type SortDir = "asc" | "desc";

function sortTrades(trades: ClosedTrade[], key: SortKey, dir: SortDir): ClosedTrade[] {
  const sorted = [...trades].sort((a, b) => {
    const av = a[key] ?? "";
    const bv = b[key] ?? "";
    if (typeof av === "number" && typeof bv === "number") return av - bv;
    return String(av).localeCompare(String(bv));
  });
  return dir === "desc" ? sorted.reverse() : sorted;
}

function ClosedTradesTab({ trades }: { trades: ClosedTrade[] }) {
  const [sortKey, setSortKey] = useState<SortKey>("closed_at");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  if (trades.length === 0) return <div className="empty-state">No closed trades yet. Trades appear here when positions are closed.</div>;

  const totalPnl = trades.reduce((s, t) => s + t.pnl_usd, 0);
  const winners = trades.filter((t) => t.is_winner).length;
  const sorted = sortTrades(trades, sortKey, sortDir);

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const arrow = (key: SortKey) => (sortKey === key ? (sortDir === "asc" ? " ▲" : " ▼") : "");

  const thStyle: React.CSSProperties = { cursor: "pointer", userSelect: "none", whiteSpace: "nowrap" };

  return (
    <div>
      <div className="stat-grid" style={{ marginBottom: "1rem" }}>
        <div className="stat-card">
          <div className="label">Closed Trades</div>
          <div className="value">{trades.length}</div>
        </div>
        <div className="stat-card">
          <div className="label">Win Rate</div>
          <div className="value" style={{ color: winners / trades.length >= 0.5 ? "var(--green)" : "var(--red)" }}>
            {((winners / trades.length) * 100).toFixed(0)}%
          </div>
        </div>
        <div className="stat-card">
          <div className="label">Total Realized PnL</div>
          <div className={`value ${totalPnl >= 0 ? "pnl-positive" : "pnl-negative"}`}>
            ${totalPnl >= 0 ? "+" : ""}{totalPnl.toFixed(2)}
          </div>
        </div>
      </div>
      <div style={{ overflowX: "auto", maxHeight: 500, overflowY: "auto" }}>
        <table>
          <thead>
            <tr>
              <th style={thStyle} onClick={() => toggleSort("symbol")}>Symbol{arrow("symbol")}</th>
              <th style={thStyle} onClick={() => toggleSort("side")}>Side{arrow("side")}</th>
              <th style={thStyle} onClick={() => toggleSort("strategy")}>Strategy{arrow("strategy")}</th>
              <th style={thStyle} onClick={() => toggleSort("entry_price")}>Entry{arrow("entry_price")}</th>
              <th style={thStyle} onClick={() => toggleSort("exit_price")}>Exit{arrow("exit_price")}</th>
              <th style={thStyle} onClick={() => toggleSort("pnl_usd")}>PnL{arrow("pnl_usd")}</th>
              <th style={thStyle} onClick={() => toggleSort("leverage")}>Leverage{arrow("leverage")}</th>
              <th style={thStyle} onClick={() => toggleSort("scale_mode")}>Mode{arrow("scale_mode")}</th>
              <th style={thStyle} onClick={() => toggleSort("dca_count")}>DCAs{arrow("dca_count")}</th>
              <th style={thStyle} onClick={() => toggleSort("hold_minutes")}>Hold Time{arrow("hold_minutes")}</th>
              <th style={thStyle} onClick={() => toggleSort("closed_at")}>Closed{arrow("closed_at")}</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((t) => (
              <tr key={t.id}>
                <td><strong>{t.symbol}</strong></td>
                <td style={{ color: t.side === "buy" || t.side === "long" ? "var(--green)" : "var(--red)" }}>
                  {t.side.toUpperCase()}
                </td>
                <td style={{ fontSize: "0.85rem" }}>{t.strategy}</td>
                <td>{t.entry_price < 1 ? t.entry_price.toFixed(6) : t.entry_price.toFixed(2)}</td>
                <td>{t.exit_price < 1 ? t.exit_price.toFixed(6) : t.exit_price.toFixed(2)}</td>
                <td className={t.pnl_usd >= 0 ? "pnl-positive" : "pnl-negative"} style={{ fontWeight: 600 }}>
                  {t.pnl_pct >= 0 ? "+" : ""}{t.pnl_pct.toFixed(2)}%
                  <br />
                  <span style={{ fontSize: "0.8rem" }}>${t.pnl_usd >= 0 ? "+" : ""}{t.pnl_usd.toFixed(2)}</span>
                </td>
                <td>{t.leverage}x</td>
                <td style={{ fontSize: "0.8rem" }}>{t.scale_mode || "—"}</td>
                <td>{t.dca_count}</td>
                <td>{t.hold_minutes < 60 ? `${t.hold_minutes.toFixed(0)}m` : `${(t.hold_minutes / 60).toFixed(1)}h`}</td>
                <td style={{ fontSize: "0.8rem" }}>
                  {t.closed_at ? new Date(t.closed_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function StrategyScoresTab({ scores }: { scores: StrategyScore[] }) {
  if (scores.length === 0) return <div className="empty-state">No strategy stats yet — stats populate as positions close.</div>;

  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>Strategy</th>
            <th>Trades</th>
            <th>Win Rate</th>
            <th>PF</th>
            <th>Total PnL</th>
            <th>Avg Win</th>
            <th>Avg Loss</th>
            <th>Weight</th>
            <th>Streak</th>
            <th>Best / Worst Hour</th>
            <th>Best / Worst Regime</th>
          </tr>
        </thead>
        <tbody>
          {scores.map((s) => (
            <tr key={s.strategy}>
              <td><strong>{s.strategy}</strong></td>
              <td>{s.total_trades} <span style={{ color: "var(--text-muted)", fontSize: "0.75rem" }}>({s.winners}W {s.losers}L)</span></td>
              <td style={{ color: s.win_rate >= 0.5 ? "var(--green)" : s.win_rate >= 0.35 ? "var(--yellow)" : "var(--red)" }}>
                {(s.win_rate * 100).toFixed(0)}%
              </td>
              <td style={{ color: s.profit_factor >= 1.5 ? "var(--green)" : s.profit_factor >= 1 ? "var(--text)" : "var(--red)" }}>
                {s.profit_factor.toFixed(2)}
              </td>
              <td className={s.total_pnl >= 0 ? "pnl-positive" : "pnl-negative"}>
                ${s.total_pnl.toFixed(2)}
              </td>
              <td className="pnl-positive">+{s.avg_win_pct.toFixed(2)}%</td>
              <td className="pnl-negative">{s.avg_loss_pct.toFixed(2)}%</td>
              <td style={{ color: weightColor(s.weight), fontWeight: 600 }}>
                {s.weight.toFixed(2)}x
              </td>
              <td style={{ color: s.streak_current > 0 ? "var(--green)" : s.streak_current < -2 ? "var(--red)" : "var(--text)" }}>
                {s.streak_current > 0 ? `+${s.streak_current}W` : s.streak_current < 0 ? `${Math.abs(s.streak_current)}L` : "—"}
                {s.streak_max_loss >= 5 && (
                  <span style={{ fontSize: "0.7rem", color: "var(--red)", marginLeft: 4 }}>
                    (max: {s.streak_max_loss}L)
                  </span>
                )}
              </td>
              <td style={{ fontSize: "0.8rem" }}>
                {s.best_hour_utc >= 0 ? `${s.best_hour_utc}:00` : "—"} / {s.worst_hour_utc >= 0 ? `${s.worst_hour_utc}:00` : "—"}
              </td>
              <td style={{ fontSize: "0.8rem" }}>
                {s.best_regime || "—"} / {s.worst_regime || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PatternsTab({ patterns }: { patterns: PatternInsight[] }) {
  if (patterns.length === 0) return <div className="empty-state">No patterns detected. Keep trading and patterns will emerge.</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
      {patterns.map((p, i) => (
        <div key={i} className="card" style={{ borderLeft: `3px solid ${SEVERITY_COLORS[p.severity] || "var(--border)"}` }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.3rem" }}>
            <span className="badge" style={{
              background: `${SEVERITY_COLORS[p.severity]}22`,
              color: SEVERITY_COLORS[p.severity],
            }}>
              {p.severity.toUpperCase()}
            </span>
            <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
              {p.sample_size} trades · {(p.confidence * 100).toFixed(0)}% confidence
            </span>
          </div>
          <div style={{ color: "var(--heading)", fontWeight: 500, marginBottom: "0.3rem" }}>
            {p.description}
          </div>
          {p.suggestion && (
            <div style={{ fontSize: "0.85rem", color: "var(--accent)" }}>
              Suggestion: {p.suggestion}
            </div>
          )}
          {(p.affected_strategy || p.affected_symbol) && (
            <div style={{ fontSize: "0.8rem", color: "var(--text-muted)", marginTop: "0.2rem" }}>
              {p.affected_strategy && `Strategy: ${p.affected_strategy}`}
              {p.affected_symbol && ` · Symbol: ${p.affected_symbol}`}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function SuggestionsTab({
  suggestions,
  onStatusUpdate,
  busySuggestionId,
}: {
  suggestions: ModSuggestion[];
  onStatusUpdate: (id: number, status: string) => Promise<void>;
  busySuggestionId: number | null;
}) {
  if (suggestions.length === 0) return <div className="empty-state">No modification suggestions. All strategies performing within acceptable bounds.</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
      {suggestions.map((s, i) => (
        <div key={i} className="card" style={{ borderLeft: `3px solid ${SUGGESTION_COLORS[s.suggestion_type] || "var(--accent)"}` }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.3rem" }}>
            <span style={{ color: "var(--heading)", fontWeight: 600, fontSize: "1rem" }}>
              {s.title}
            </span>
            <div style={{ display: "flex", gap: "0.4rem" }}>
              <span className="badge" style={{
                background: s.source === "openclaw" ? "rgba(147, 112, 219, 0.2)" : "rgba(88, 166, 255, 0.2)",
                color: s.source === "openclaw" ? "var(--purple)" : "var(--accent)",
              }}>
                {s.source === "openclaw" ? "OPENCLAW" : "ANALYTICS"}
              </span>
              <span className="badge" style={{
                background: `${SUGGESTION_COLORS[s.suggestion_type] || "var(--accent)"}22`,
                color: SUGGESTION_COLORS[s.suggestion_type] || "var(--accent)",
              }}>
                {s.suggestion_type.replace("_", " ").toUpperCase()}
              </span>
              <span className="badge">{(s.status || "new").toUpperCase()}</span>
            </div>
          </div>
          <div style={{ marginBottom: "0.4rem" }}>{s.description}</div>
          {(s.current_value || s.suggested_value) && (
            <div style={{ display: "flex", gap: "2rem", fontSize: "0.85rem", marginBottom: "0.3rem" }}>
              {s.current_value && (
                <span>Current: <code style={{ color: "var(--red)" }}>{s.current_value}</code></span>
              )}
              {s.suggested_value && (
                <span>Suggested: <code style={{ color: "var(--green)" }}>{s.suggested_value}</code></span>
              )}
            </div>
          )}
          {s.expected_improvement && (
            <div style={{ fontSize: "0.85rem", color: "var(--green)" }}>
              Expected: {s.expected_improvement}
            </div>
          )}
          <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "0.3rem" }}>
            Based on {s.based_on_trades} trades · {(s.confidence * 100).toFixed(0)}% confidence
            {s.updated_at ? ` · ${new Date(s.updated_at).toLocaleString()}` : ""}
          </div>
          {s.source === "openclaw" && s.id > 0 && (
            <div style={{ display: "flex", gap: "0.4rem", marginTop: "0.55rem" }}>
              <button
                disabled={busySuggestionId === s.id}
                className="btn-secondary"
                onClick={() => onStatusUpdate(s.id, "accepted")}
              >
                Accept
              </button>
              <button
                disabled={busySuggestionId === s.id}
                className="btn-secondary"
                onClick={() => onStatusUpdate(s.id, "implemented")}
              >
                Implemented
              </button>
              <button
                disabled={busySuggestionId === s.id}
                className="btn-secondary"
                onClick={() => onStatusUpdate(s.id, "removed")}
              >
                Remove
              </button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function HourlyTab({ hourly, regime }: {
  hourly: Array<{ hour_utc: number; trades: number; wins: number; avg_pnl: number; total_pnl: number }>;
  regime: Array<{ market_regime: string; trades: number; wins: number; avg_pnl: number; total_pnl: number }>;
}) {
  const maxPnl = Math.max(1, ...hourly.map((h) => Math.abs(h.total_pnl)));

  return (
    <div>
      <h3 style={{ color: "var(--heading)", marginBottom: "0.5rem" }}>Performance by Hour (UTC)</h3>
      {hourly.length === 0 ? (
        <div className="empty-state">No hourly data yet</div>
      ) : (
        <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 150, marginBottom: "2rem", padding: "0 1rem" }}>
          {hourly.map((h) => {
            const barHeight = Math.max(4, (Math.abs(h.total_pnl) / maxPnl) * 120);
            const isPositive = h.total_pnl >= 0;
            return (
              <div key={h.hour_utc} style={{ display: "flex", flexDirection: "column", alignItems: "center", flex: 1 }}
                title={`${h.hour_utc}:00 - ${h.trades} trades, ${h.wins} wins, $${h.total_pnl.toFixed(0)}`}>
                <div style={{
                  width: "100%", maxWidth: 30, height: barHeight,
                  background: isPositive ? "var(--green)" : "var(--red)",
                  borderRadius: "3px 3px 0 0", opacity: 0.8,
                }} />
                <span style={{ fontSize: "0.65rem", color: "var(--text-muted)", marginTop: 2 }}>
                  {h.hour_utc}
                </span>
              </div>
            );
          })}
        </div>
      )}

      <h3 style={{ color: "var(--heading)", marginBottom: "0.5rem" }}>Performance by Market Regime</h3>
      {regime.length === 0 ? (
        <div className="empty-state">No regime data yet</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Regime</th>
              <th>Trades</th>
              <th>Win Rate</th>
              <th>Avg PnL %</th>
              <th>Total PnL</th>
            </tr>
          </thead>
          <tbody>
            {regime.map((r) => {
              const wr = r.trades > 0 ? r.wins / r.trades : 0;
              return (
                <tr key={r.market_regime}>
                  <td><strong>{r.market_regime}</strong></td>
                  <td>{r.trades}</td>
                  <td style={{ color: wr >= 0.5 ? "var(--green)" : "var(--red)" }}>{(wr * 100).toFixed(0)}%</td>
                  <td className={r.avg_pnl >= 0 ? "pnl-positive" : "pnl-negative"}>
                    {r.avg_pnl >= 0 ? "+" : ""}{r.avg_pnl.toFixed(2)}%
                  </td>
                  <td className={r.total_pnl >= 0 ? "pnl-positive" : "pnl-negative"}>
                    ${r.total_pnl.toFixed(2)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ReportTab({ report }: { report: DailyReport | null }) {
  if (!report) return <div className="empty-state">No report data</div>;

  return (
    <div>
      {report.projected && (
        <div className="stat-grid" style={{ marginBottom: "1.5rem" }}>
          <div className="stat-card">
            <div className="label">1 Week Projection</div>
            <div className="value">${report.projected["1_week"]?.toLocaleString(undefined, { maximumFractionDigits: 0 }) ?? "—"}</div>
          </div>
          <div className="stat-card">
            <div className="label">1 Month Projection</div>
            <div className="value">${report.projected["1_month"]?.toLocaleString(undefined, { maximumFractionDigits: 0 }) ?? "—"}</div>
          </div>
          <div className="stat-card">
            <div className="label">3 Month Projection</div>
            <div className="value">${report.projected["3_months"]?.toLocaleString(undefined, { maximumFractionDigits: 0 }) ?? "—"}</div>
          </div>
        </div>
      )}

      {report.history.length > 0 && (
        <>
          <h3 style={{ color: "var(--heading)", marginBottom: "0.5rem" }}>Daily History</h3>
          <div style={{ maxHeight: 300, overflowY: "auto", marginBottom: "1.5rem" }}>
            <table>
              <thead>
                <tr><th>Day</th><th>Date</th><th>Start</th><th>End</th><th>PnL %</th><th>Trades</th><th>Target</th></tr>
              </thead>
              <tbody>
                {[...report.history].reverse().map((r) => (
                  <tr key={r.day}>
                    <td>{r.day}</td>
                    <td>{r.date}</td>
                    <td>${r.start_balance.toFixed(2)}</td>
                    <td>${r.end_balance.toFixed(2)}</td>
                    <td className={r.pnl_pct >= 0 ? "pnl-positive" : "pnl-negative"}>
                      {r.pnl_pct >= 0 ? "+" : ""}{r.pnl_pct.toFixed(2)}%
                    </td>
                    <td>{r.trades}</td>
                    <td>{r.target_hit ? "HIT" : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <h3 style={{ color: "var(--heading)", marginBottom: "0.5rem" }}>Compound Report</h3>
      <pre className="report">{report.compound_report}</pre>
    </div>
  );
}
