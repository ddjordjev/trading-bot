import { useEffect, useRef, useState, useCallback } from "react";
import { wsUrl } from "../api/client";

export interface BotSnapshot {
  bot_id: string;
  exchange: string;
  connected: boolean;
  data: {
    status: BotStatus;
    positions: PositionInfo[];
    wick_scalps: WickScalpInfo[];
    intel: IntelSnapshot | null;
    logs: LogEntry[];
  } | null;
}

export interface FullSnapshot {
  status: BotStatus;
  positions: (PositionInfo & { bot_id?: string; exchange_name?: string })[];
  intel: IntelSnapshot | null;
  wick_scalps: (WickScalpInfo & { bot_id?: string; exchange_name?: string })[];
  logs: LogEntry[];
  bots?: BotSnapshot[];
  exchange_balances?: Record<string, number>;
}

export interface BotStatus {
  bot_id: string;
  running: boolean;
  trading_mode: string;
  exchange_name: string;
  exchange_url: string;
  balance: number;
  available_margin: number;
  daily_pnl: number;
  daily_pnl_pct: number;
  tier: string;
  tier_progress_pct: number;
  daily_target_pct: number;
  total_growth_pct: number;
  total_growth_usd: number;
  uptime_seconds: number;
  manual_stop_active: boolean;
  strategies_count: number;
  dynamic_strategies_count: number;
  profit_buffer_pct: number;
}

export interface PositionInfo {
  symbol: string;
  side: string;
  amount: number;
  entry_price: number;
  current_price: number;
  pnl_pct: number;
  pnl_usd: number;
  leverage: number;
  market_type: string;
  strategy: string;
  stop_loss: number | null;
  take_profit: number | null;
  exchange_stop_loss: number | null;
  exchange_take_profit: number | null;
  bot_stop_loss: number | null;
  bot_take_profit: number | null;
  effective_stop_loss: number | null;
  effective_take_profit: number | null;
  stop_source: string;
  tp_source: string;
  risk_state: string;
  close_pending: boolean;
  close_reason_pending: string;
  notional_value: number;
  age_minutes: number;
  breakeven_locked: boolean;
  scale_mode: string;
  scale_phase: string;
  dca_count: number;
  trade_url: string;
}

export interface MacroEventInfo {
  title: string;
  impact: string;
  hours_until: number;
  date_iso: string;
}

export interface IntelSnapshot {
  regime: string;
  fear_greed: number;
  fear_greed_bias: string;
  liquidation_24h: number;
  mass_liquidation: boolean;
  liquidation_bias: string;
  macro_event_imminent: boolean;
  macro_exposure_mult: number;
  macro_spike_opportunity: boolean;
  next_macro_event: string;
  macro_events: MacroEventInfo[];
  whale_bias: string;
  overleveraged_side: string;
  position_size_multiplier: number;
  should_reduce_exposure: boolean;
  preferred_direction: string;
  openclaw_regime: string;
  openclaw_regime_confidence: number;
  openclaw_regime_why: string[];
  openclaw_sentiment_score: number;
  openclaw_long_short_ratio: number;
  openclaw_liquidations_24h_usd: number;
  openclaw_open_interest_24h_usd: number;
  openclaw_idea_briefs: Record<string, unknown>[];
  openclaw_failure_triage: Record<string, unknown>[];
  openclaw_experiments: Record<string, unknown>[];
  source_timestamps?: Record<string, string>;
  sources_active?: string[];
}

export interface WickScalpInfo {
  symbol: string;
  scalp_side: string;
  entry_price: number;
  amount: number;
  age_minutes: number;
  max_hold_minutes: number;
}

export interface LogEntry {
  ts: string;
  level: string;
  msg: string;
  module: string;
}

export function useWebSocket() {
  const [data, setData] = useState<FullSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<number>(0);
  const mountedRef = useRef(true);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      retryRef.current = 0;
    };
    ws.onmessage = (e) => {
      try { setData(JSON.parse(e.data)); } catch {}
    };
    ws.onclose = () => {
      setConnected(false);
      if (!mountedRef.current) return;
      const delay = Math.min(1000 * 2 ** retryRef.current, 15000);
      retryRef.current++;
      setTimeout(connect, delay);
    };
    ws.onerror = () => ws.close();
  }, []);

  const reconnect = useCallback(() => {
    retryRef.current = 0;
    wsRef.current?.close();
    setData(null);
    setTimeout(connect, 100);
  }, [connect]);

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      wsRef.current?.close();
    };
  }, [connect]);

  return { data, connected, reconnect };
}
