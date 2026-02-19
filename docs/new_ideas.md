# New Strategy Ideas — Discussion Notes

> Captured: 2026-02-19 | Status: Under review | Not yet implemented

---

## Idea 1: Volatility Straddle (Simultaneous Long + Short)

### User's Approach

Open both a long and short on the same asset simultaneously with high leverage
(e.g. $15,000 notional at 300x = ~$50 capital per leg). Wait for a directional
move (typically at market open), then manage exits asymmetrically — the winning
side rides while the losing side gets cut early. The spread between the two
exits is the profit.

User noted: had some success but not consistently. Was exploiting MEXC's
zero-fee promotion — **fees are critical to this strategy's viability**.

### Known As

Volatility straddle / delta-neutral hedge. Standard derivatives strategy
adapted for crypto perpetuals.

### What Exists Already

- `HedgeManager` (`core/orders/hedge.py`) — defensive counter-position on
  existing profitable trades. Different intent (protect gains, not pre-position
  for volatility).
- `WickScalpDetector` (`core/orders/wick_scalp.py`) — reactive counter-scalps
  during PYRAMID drawdowns. Also different (reactive, not pre-positioned).

### Assessment

**Pros:**
- Guaranteed to be on the right side of a big move
- With extreme leverage, small moves generate meaningful P&L
- Pairs naturally with market-open windows where directional moves are expected

**Cons:**
- **Fees destroy it.** At 300x, even 0.05% maker/taker = 15% of position value
  per side. Only viable with zero/near-zero fees.
- **Whipsaw risk.** If price spikes one way (stopping the loser) then reverses
  (stopping the winner), both legs lose. Market opens are exactly when this
  can happen.
- **Slippage** on high-leverage fills during fast moves.
- Exit placement is everything — depends on current ATR/volatility, hard to
  generalize.

### Implementation Notes

If we build this:
- Dedicated `StraddleStrategy` class, triggered only during market-open windows
- Volatility filter: only fire when ATR is expanding (not in dead-flat markets)
- Whipsaw guard: if one leg is stopped, widen the other leg's stop temporarily
- **Fee-aware**: calculate expected edge minus fees before entering. Skip if
  fees > expected edge.
- Configurable leverage per leg (default high, e.g. 100-300x)
- Tie to `MarketOpenVolatilityStrategy` schedule for timing

---

## Idea 2: Super Scalp (High Volume, $1-per-Trade Target)

### User's Request

> "If you manage to get 500 trades for me with $1 gain in each, I'll be very
> happy. 10% or not, $500 is $500."

Lots of small successful trades are very acceptable. Volume over size.

### What Exists Already

- `CompoundMomentumStrategy` — does scalping (spike + breakout, 8-15 min hold)
  but optimized for entry quality, not trade volume.
- `WickScalpDetector` — counter-trades on wicks, very short hold (5 min, 0.3%
  trail). Close but only fires during existing PYRAMID drawdowns.

### Assessment

**Pros:**
- Math is appealing: 500 × $1 = $500 even at modest win rates
- Small gains compound well with the existing daily target system
- Diversifies risk across many small bets instead of few large ones

**Cons:**
- **Latency.** Current tick loop is 60 seconds. To scalp $1 on small positions
  you need sub-second price feeds and near-instant execution. 50-100
  trades/day is realistic with current architecture; 500 requires faster loops.
- **Fees.** 500 round-trips = 1000 orders. At 0.02% per trade on $500 notional,
  that's ~$100 in fees. Must track fee cost per trade and ensure edge > fees.
- **Exchange rate limits.** MEXC/Binance have order placement rate limits that
  cap throughput.
- **Risk/reward at 0.2% targets** is ~1:1, so win rate must be >50%.

### Implementation Notes

If we build this:
- New `SuperScalpStrategy` targeting 0.1-0.3% moves with higher leverage
- Use limit orders (maker fees are lower or zero) instead of market orders
- Minimum hold: 1-3 candles. Maximum hold: 5-10 minutes.
- Only activate during high-liquidity windows (US session, overlap periods)
- Track fees per trade; skip if expected edge < fee cost
- Consider tightening tick loop to 15-30 seconds for this strategy specifically
- Start conservatively: target 50-100 trades/day, scale up if profitable

---

## Idea 3: Don't Fight the Trend (3rd Holy Rule)

### User's Request

> "ADD 3rd HOLY RULE to cut the losers, ride the winners pair, and that is
> DON'T FIGHT THE TREND. At least don't fight it for anything that you plan
> to execute longer than 1 day. It's ok for scalp trades but anything opened
> for longer term should take this into account."

### The Three Holy Rules

1. **Cut the losers** — never let a bad trade run hoping it'll recover
2. **Ride the winners** — never close a winner on time expiry, only trailing stops
3. **Don't fight the trend** — non-scalp trades must align with the macro trend

### What Exists Already

- `MarketIntel.assess()` computes `preferred_direction` (long/short/neutral)
  from 10 data sources via voting. But this is a **soft influence** (signal
  boost/penalty), not a hard gate.
- `MarketRegime` (RISK_ON, NORMAL, CAUTION, RISK_OFF, CAPITULATION) describes
  risk appetite, not bull/bear trend.
- TradingView 1h/4h/1D consensus is the closest to trend detection but used
  only as a multiplier, not a blocker.

**Gap:** No explicit "we are in a bull/bear market" classifier. No hard rule
that blocks counter-trend trades beyond scalps.

### Assessment

**This is the most impactful of the three ideas.** It codifies what every
experienced trader knows but bots often ignore. The current system can and
does open multi-day longs in a clear downtrend if strategy signals fire.

The user's distinction is key:
- **Scalps (< 1 day):** Trend doesn't matter. In and out too fast.
- **Anything longer:** Must align with macro trend or don't trade it.

### Implementation Notes

Build a `MacroTrend` classifier using:
- BTC daily SMA(50) vs SMA(200) — golden cross = bull, death cross = bear
- BTC weekly TradingView consensus (1D + 1W timeframes)
- DeFiLlama TVL trend (capital inflows = bull, outflows = bear)
- Glassnode accumulation vs distribution phase
- Fear & Greed 30-day rolling average (persistent fear = bear, greed = bull)

Enforcement:
- Add `macro_trend` field to `MarketCondition` (enum: BULL, BEAR, NEUTRAL)
- In the bot tick loop, after signal generation: if signal's expected hold
  > N hours AND signal direction opposes macro_trend → **reject it**
- Scalps and wick trades pass through regardless
- Log every rejection so we can verify it's not being too aggressive

Add to `Signal` model:
- Consider adding `expected_hold_duration` field so the filter knows which
  signals are scalps vs longer-term

---

## Suggested Implementation Order

1. **#3 — Don't Fight the Trend** — highest impact, reinforces existing
   philosophy, clean integration into current gate system
2. **#1 — Volatility Straddle** — new strategy, needs fee/whipsaw logic,
   pairs with market-open windows
3. **#2 — Super Scalp** — most architectural work (tick loop speed, fee
   tracking, order throughput)

---

## Global Reminder: Fee Awareness

Ideas #1 and #2 both hinge on fee economics. The bot should:
- Track actual fees paid per trade (not just estimated)
- Store fee data in `trades.db` alongside P&L
- Surface fee impact in analytics dashboard
- Have a configurable `min_edge_after_fees` threshold
- Be aware of exchange fee promotions (MEXC zero-fee periods, etc.)
