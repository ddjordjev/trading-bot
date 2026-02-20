# Trading Bot — Daily Log

Starting capital: $1000 x 3 bots | Mode: paper_local | Start date: 2026-02-20

---

<!-- Append daily entries below. Most recent at the top. -->

## Day 0 — 2026-02-20 (Pre-Launch + Start)

**Mode:** paper_local (local simulation, no exchange orders)
**Balance:** $1000 each x 3 bots = $3000 total
**BTC Price:** $67,676

### Setup

| Bot | Strategies | Dashboard | Style |
|-----|-----------|-----------|-------|
| bot-momentum | compound_momentum, market_open_volatility, rsi, macd | :9035 | momentum |
| bot-meanrev | bollinger, mean_reversion, grid | :9036 | meanrev |
| bot-swing | swing_opportunity | :9037 | swing |

### Supporting Services
- Monitor (7 intel sources: TV, CMC, CoinGecko, F&G, Liquidations, Macro, Whale)
- Analytics (strategy scoring)
- DB Sync (merges per-bot trade DBs)
- Grafana (:3001), Prometheus, Loki/Promtail (log aggregation)

### Pre-Launch Results
- Preflight: 15/15 PASS
- Audit: all 4 groups CLEAN (0 bugs, 55 total fixed historically)
- Docker: all 10 containers running, 3 bots healthy

### Early Observations (first 10 min)
- Monitor detecting hot movers: VANA/USDT (+27%), AZTEC (+60% 24h), BIO (+40% 24h)
- Momentum bot already traded VANA/USDT (opened, closed at -$15.91, re-opened)
- Meanrev bot scanning but no signals yet (needs technical conditions)
- Swing bot scanning but no positions yet (longer timeframe)
- Minor: db-sync shows "unhealthy" (Dockerfile healthcheck curls :9035, db-sync has no HTTP server — cosmetic only)

### Risk Config (paper_local relaxed)
- Max position size: 50% ($500)
- Max daily loss: 100% (relaxed for paper)
- Max concurrent positions: 10 per bot
- Signal strength threshold: 0.2 (low, to allow more trades)
- Leverage: 10x default

### Notes
- 24-hour run started ~21:20 UTC
- Changes during run: code fixes pushed to GitHub but not redeployed
- Monitoring: periodic log checks and status reports
