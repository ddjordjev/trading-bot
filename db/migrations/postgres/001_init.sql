CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    bot_id TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy TEXT NOT NULL,
    action TEXT NOT NULL,
    scale_mode TEXT NOT NULL DEFAULT '',
    entry_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    exit_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    amount DOUBLE PRECISION NOT NULL DEFAULT 0,
    leverage INTEGER NOT NULL DEFAULT 1,
    pnl_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    pnl_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
    is_winner INTEGER NOT NULL DEFAULT 0,
    hold_minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
    was_quick_trade INTEGER NOT NULL DEFAULT 0,
    was_low_liquidity INTEGER NOT NULL DEFAULT 0,
    dca_count INTEGER NOT NULL DEFAULT 0,
    max_drawdown_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
    market_regime TEXT NOT NULL DEFAULT '',
    fear_greed INTEGER NOT NULL DEFAULT 50,
    daily_tier TEXT NOT NULL DEFAULT '',
    daily_pnl_at_entry DOUBLE PRECISION NOT NULL DEFAULT 0,
    signal_strength DOUBLE PRECISION NOT NULL DEFAULT 0,
    hour_utc INTEGER NOT NULL DEFAULT 0,
    day_of_week INTEGER NOT NULL DEFAULT 0,
    volatility_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
    opened_at TEXT NOT NULL DEFAULT '',
    closed_at TEXT NOT NULL DEFAULT '',
    planned_stop_loss DOUBLE PRECISION NOT NULL DEFAULT 0,
    planned_tp1 DOUBLE PRECISION NOT NULL DEFAULT 0,
    planned_tp2 DOUBLE PRECISION NOT NULL DEFAULT 0,
    exchange_stop_loss DOUBLE PRECISION NOT NULL DEFAULT 0,
    exchange_take_profit DOUBLE PRECISION NOT NULL DEFAULT 0,
    bot_stop_loss DOUBLE PRECISION NOT NULL DEFAULT 0,
    bot_take_profit DOUBLE PRECISION NOT NULL DEFAULT 0,
    effective_stop_loss DOUBLE PRECISION NOT NULL DEFAULT 0,
    effective_take_profit DOUBLE PRECISION NOT NULL DEFAULT 0,
    stop_source TEXT NOT NULL DEFAULT 'none',
    tp_source TEXT NOT NULL DEFAULT 'none',
    close_source TEXT NOT NULL DEFAULT '',
    close_reason TEXT NOT NULL DEFAULT '',
    exchange_close_order_id TEXT NOT NULL DEFAULT '',
    exchange_close_trade_id TEXT NOT NULL DEFAULT '',
    close_detected_at TEXT NOT NULL DEFAULT '',
    request_key TEXT NOT NULL DEFAULT '',
    recovery_close INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_winner ON trades(is_winner);
CREATE INDEX IF NOT EXISTS idx_trades_close_source ON trades(close_source);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_closed ON trades(symbol, closed_at);
CREATE INDEX IF NOT EXISTS idx_hub_bot ON trades(bot_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hub_reqkey ON trades(request_key) WHERE request_key <> '';

CREATE TABLE IF NOT EXISTS bot_config (
    bot_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS exchange_symbols (
    exchange TEXT PRIMARY KEY,
    symbols TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cex_binance_snapshots (
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    quote_volume DOUBLE PRECISION NOT NULL,
    change_24h DOUBLE PRECISION NOT NULL,
    funding_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (timestamp, symbol)
);
CREATE INDEX IF NOT EXISTS idx_cex_binance_ts ON cex_binance_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_cex_binance_symbol_ts ON cex_binance_snapshots(symbol, timestamp);

CREATE TABLE IF NOT EXISTS cex_binance_symbol_state (
    symbol TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    last_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_quote_volume DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_change_24h DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_funding_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    avg_quote_volume DOUBLE PRECISION NOT NULL DEFAULT 0,
    vol_accel DOUBLE PRECISION NOT NULL DEFAULT 0,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    score DOUBLE PRECISION NOT NULL DEFAULT 0,
    chg_1m DOUBLE PRECISION NOT NULL DEFAULT 0,
    chg_5m DOUBLE PRECISION NOT NULL DEFAULT 0,
    chg_1h DOUBLE PRECISION NOT NULL DEFAULT 0,
    chg_4h DOUBLE PRECISION NOT NULL DEFAULT 0,
    chg_1d DOUBLE PRECISION NOT NULL DEFAULT 0,
    chg_1w DOUBLE PRECISION NOT NULL DEFAULT 0,
    chg_3w DOUBLE PRECISION NOT NULL DEFAULT 0,
    chg_1mo DOUBLE PRECISION NOT NULL DEFAULT 0,
    chg_3mo DOUBLE PRECISION NOT NULL DEFAULT 0,
    chg_1y DOUBLE PRECISION NOT NULL DEFAULT 0,
    anchor_1m_ts TEXT NOT NULL DEFAULT '',
    anchor_5m_ts TEXT NOT NULL DEFAULT '',
    anchor_1h_ts TEXT NOT NULL DEFAULT '',
    anchor_4h_ts TEXT NOT NULL DEFAULT '',
    anchor_1d_ts TEXT NOT NULL DEFAULT '',
    anchor_1w_ts TEXT NOT NULL DEFAULT '',
    anchor_3w_ts TEXT NOT NULL DEFAULT '',
    anchor_1mo_ts TEXT NOT NULL DEFAULT '',
    anchor_3mo_ts TEXT NOT NULL DEFAULT '',
    anchor_1y_ts TEXT NOT NULL DEFAULT '',
    anchor_1m_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    anchor_5m_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    anchor_1h_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    anchor_4h_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    anchor_1d_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    anchor_1w_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    anchor_3w_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    anchor_1mo_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    anchor_3mo_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    anchor_1y_price DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cex_binance_state_updated_at ON cex_binance_symbol_state(updated_at);

CREATE TABLE IF NOT EXISTS openclaw_daily_reports (
    id BIGSERIAL PRIMARY KEY,
    report_day TEXT NOT NULL,
    run_kind TEXT NOT NULL DEFAULT 'scheduled',
    requested_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    lane_used TEXT NOT NULL DEFAULT 'fallback',
    status TEXT NOT NULL DEFAULT 'ok',
    source_url TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_openclaw_reports_completed ON openclaw_daily_reports(completed_at);

CREATE TABLE IF NOT EXISTS openclaw_suggestions (
    id BIGSERIAL PRIMARY KEY,
    suggestion_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'openclaw',
    status TEXT NOT NULL DEFAULT 'new',
    suggestion_type TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    current_value TEXT NOT NULL DEFAULT '',
    suggested_value TEXT NOT NULL DEFAULT '',
    expected_improvement TEXT NOT NULL DEFAULT '',
    based_on_trades INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    first_seen_report_id INTEGER NOT NULL DEFAULT 0,
    last_seen_report_id INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    implemented_at TEXT NOT NULL DEFAULT '',
    removed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_openclaw_suggestions_status ON openclaw_suggestions(status, updated_at);

CREATE TABLE IF NOT EXISTS exchange_equity_snapshots (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    available_usdt DOUBLE PRECISION NOT NULL DEFAULT 0,
    estimated_equity_usdt DOUBLE PRECISION NOT NULL DEFAULT 0,
    open_positions INTEGER NOT NULL DEFAULT 0,
    source_bot TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'bot_report',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exchange_equity_snapshots_ex_ts ON exchange_equity_snapshots(exchange, created_at);

CREATE TABLE IF NOT EXISTS analytics_snapshots (
    id BIGSERIAL PRIMARY KEY,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    total_trades_logged INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analytics_snapshots_updated_at ON analytics_snapshots(updated_at);

CREATE TABLE IF NOT EXISTS swing_entry_plans (
    id BIGSERIAL PRIMARY KEY,
    bot_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    entry_idx INTEGER NOT NULL DEFAULT 0,
    side TEXT NOT NULL DEFAULT '',
    price DOUBLE PRECISION NOT NULL DEFAULT 0,
    amount DOUBLE PRECISION NOT NULL DEFAULT 0,
    leverage INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'planned',
    order_id TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_swing_entry_plan_unique
    ON swing_entry_plans(bot_id, symbol, opened_at, entry_idx);
CREATE INDEX IF NOT EXISTS idx_swing_entry_plan_lookup
    ON swing_entry_plans(bot_id, symbol, opened_at);
