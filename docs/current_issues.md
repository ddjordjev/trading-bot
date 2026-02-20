# Current Issues

> Observed problems and things to fix. Add new items as bullet points.

---

- New high-intensity strategy: in/out within 1 second to 1 minute max. Picks extreme movers and rides the momentum. Ultra-fast scalping
- Manual trade ideas: ability to submit a pair, entry, SL, and TP. If small amount, bot can execute right away. Otherwise bot waits until price reaches entry zone then executes (if balance available)
- Alarm/alert system for trade entries: give bot a pair + entry price, bot sets an alarm and auto-executes when price reaches that level (if it has balance). Explore what we can use for alarm notifications
- Investigate: are news feeds actually used anywhere? Where and how do they influence trading decisions? **DONE** — news now feeds into signal weighting via `_get_news_factor()`: boosts short-term scalps, penalizes long holds (buy-rumor/sell-news), forces quick_trade when multiple headlines hit. No long-term directional bias from news.
- Why don't I see other standard strategies (Grid, etc.) active? **DONE** — all 8 strategies now registered for BTC/USDT and ETH/USDT in `main()`: compound_momentum, market_open_volatility, swing_opportunity, rsi, macd, bollinger, mean_reversion, grid.
- "No strategy data yet. Trades need to be logged first." — strategies tab shows no data even though trades have happened **DONE** — fixed 500 error from None stats + all strategies now registered.
- Bot sits idle waiting for a losing position to recover — won't open new trades. If it never recovers it waits forever. Fix: long-term positions in a loss are fine, keep trading other opportunities. Short-term positions get a time limit — if no recovery, cut and move on. Don't freeze the whole bot over a 3% loss **DONE** — blanket sit-out removed, pyramid unrealized PnL excluded from daily loss calc, stale short-term losers auto-cut after 60min, aggression halved when stale losers exist, profit buffer carries excess gains forward to expand next day's risk limits.
