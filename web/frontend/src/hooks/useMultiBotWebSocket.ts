import { useEffect, useRef, useState, useCallback } from "react";
import { wsUrlForPort } from "../api/client";
import type { FullSnapshot, BotStatus, PositionInfo, WickScalpInfo, LogEntry, IntelSnapshot } from "./useWebSocket";

export interface BotSnapshot {
  bot_id: string;
  port: number;
  connected: boolean;
  data: FullSnapshot | null;
}

export interface MergedDashboard {
  status: BotStatus;
  positions: (PositionInfo & { bot_id: string; exchange_name: string })[];
  wick_scalps: (WickScalpInfo & { bot_id: string; exchange_name: string })[];
  logs: LogEntry[];
  intel: IntelSnapshot | null;
  bots: BotSnapshot[];
}

interface BotConn {
  bot_id: string;
  port: number;
  ws: WebSocket | null;
  data: FullSnapshot | null;
  connected: boolean;
  retry: number;
}

export function useMultiBotWebSocket(botPorts: { bot_id: string; port: number }[]) {
  const [merged, setMerged] = useState<MergedDashboard | null>(null);
  const [allConnected, setAllConnected] = useState(false);
  const connsRef = useRef<BotConn[]>([]);
  const mountedRef = useRef(true);

  const rebuild = useCallback(() => {
    const conns = connsRef.current;
    const withData = conns.filter((c) => c.data !== null);
    if (withData.length === 0) {
      setMerged(null);
      setAllConnected(false);
      return;
    }

    setAllConnected(conns.every((c) => c.connected));

    const allPositions: (PositionInfo & { bot_id: string; exchange_name: string })[] = [];
    const allWicks: (WickScalpInfo & { bot_id: string; exchange_name: string })[] = [];
    const allLogs: LogEntry[] = [];
    let intel: IntelSnapshot | null = null;

    let totalBalance = 0;
    let totalAvailable = 0;
    let totalDailyPnl = 0;
    let totalDailyPnlPct = 0;
    let totalGrowthUsd = 0;
    let totalGrowthPct = 0;
    let totalProfitBuffer = 0;
    let totalUptime = 0;
    let anyRunning = false;
    let anyHalted = false;
    let strategiesCount = 0;
    let dynamicCount = 0;
    let botCount = 0;

    const firstStatus = withData[0].data!.status;

    for (const conn of withData) {
      const d = conn.data!;
      const s = d.status;
      const bid = conn.bot_id;

      totalBalance += s.balance;
      totalAvailable += s.available_margin;
      totalDailyPnl += s.daily_pnl;
      totalDailyPnlPct += s.daily_pnl_pct;
      totalGrowthUsd += s.total_growth_usd;
      totalGrowthPct += s.total_growth_pct;
      totalProfitBuffer += s.profit_buffer_pct;
      totalUptime = Math.max(totalUptime, s.uptime_seconds);
      if (s.running) anyRunning = true;
      if (s.manual_stop_active) anyHalted = true;
      strategiesCount += s.strategies_count;
      dynamicCount += s.dynamic_strategies_count;
      botCount++;

      const exName = s.exchange_name || "";
      for (const p of d.positions) allPositions.push({ ...p, bot_id: bid, exchange_name: exName });
      for (const w of d.wick_scalps) allWicks.push({ ...w, bot_id: bid, exchange_name: exName });
      for (const l of d.logs) allLogs.push(l);
      if (!intel && d.intel) intel = d.intel;
    }

    allLogs.sort((a, b) => b.ts.localeCompare(a.ts));

    const status: BotStatus = {
      bot_id: "all",
      running: anyRunning,
      trading_mode: firstStatus.trading_mode,
      exchange_name: firstStatus.exchange_name,
      exchange_url: firstStatus.exchange_url,
      balance: totalBalance,
      available_margin: totalAvailable,
      daily_pnl: totalDailyPnl,
      daily_pnl_pct: botCount > 0 ? totalDailyPnlPct / botCount : 0,
      tier: firstStatus.tier,
      tier_progress_pct: firstStatus.tier_progress_pct,
      daily_target_pct: firstStatus.daily_target_pct,
      total_growth_usd: totalGrowthUsd,
      total_growth_pct: botCount > 0 ? totalGrowthPct / botCount : 0,
      uptime_seconds: totalUptime,
      manual_stop_active: anyHalted,
      strategies_count: strategiesCount,
      dynamic_strategies_count: dynamicCount,
      profit_buffer_pct: botCount > 0 ? totalProfitBuffer / botCount : 0,
    };

    setMerged({
      status,
      positions: allPositions,
      wick_scalps: allWicks,
      logs: allLogs.slice(0, 200),
      intel,
      bots: conns.map((c) => ({
        bot_id: c.bot_id,
        port: c.port,
        connected: c.connected,
        data: c.data,
      })),
    });
  }, []);

  const connectBot = useCallback((conn: BotConn) => {
    if (!mountedRef.current) return;
    const url = wsUrlForPort(conn.port);
    const ws = new WebSocket(url);
    conn.ws = ws;

    ws.onopen = () => {
      conn.connected = true;
      conn.retry = 0;
      rebuild();
    };
    ws.onmessage = (e) => {
      try {
        conn.data = JSON.parse(e.data);
        rebuild();
      } catch {}
    };
    ws.onclose = () => {
      conn.connected = false;
      conn.ws = null;
      rebuild();
      if (!mountedRef.current) return;
      const delay = Math.min(1000 * 2 ** conn.retry, 15000);
      conn.retry++;
      setTimeout(() => connectBot(conn), delay);
    };
    ws.onerror = () => ws.close();
  }, [rebuild]);

  useEffect(() => {
    mountedRef.current = true;

    for (const old of connsRef.current) {
      old.ws?.close();
    }

    const conns: BotConn[] = botPorts.map((bp) => ({
      bot_id: bp.bot_id,
      port: bp.port,
      ws: null,
      data: null,
      connected: false,
      retry: 0,
    }));
    connsRef.current = conns;

    for (const conn of conns) {
      connectBot(conn);
    }

    return () => {
      mountedRef.current = false;
      for (const conn of conns) {
        conn.ws?.close();
      }
    };
  }, [botPorts, connectBot]);

  return { merged, allConnected };
}
