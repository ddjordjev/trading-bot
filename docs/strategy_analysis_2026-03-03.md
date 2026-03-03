# Trading Strategy Analysis — 2026-03-03

**Subject:** Strategy Implementation Review & Recommendations for Improved Results

---

## Executive Summary

Your trading strategies are implemented correctly at a technical level but use conservative, textbook parameters that may underperform in crypto’s high-volatility environment. The main opportunities are:

1. **RSI** — Period and thresholds are standard; crypto often benefits from shorter periods and wider bands.
2. **MACD** — Classic crossover logic is fine; histogram strength scaling can be improved.
3. **Bollinger** — Solid mean-reversion logic; consider adding volume confirmation.
4. **Compound Momentum** — Spike/breakout logic is reasonable; thresholds may be too loose.
5. **Swing Opportunity** — Very selective; crash thresholds and RSI levels are in a good range.

---

## 1. RSI Strategy (`strategies/rsi.py`)

**Current:** Period 14, oversold 30, overbought 70.

**Comparison:** Standard Wilder RSI. For crypto:

- **Day trading / scalping:** 7–10 period often outperforms 14.
- **Oversold/overbought:** 80/20 or 25/75 can reduce false signals in trending markets.
- **Trend filter:** No trend filter; trades against strong trends.

**Recommendations:**

- Add optional shorter period (e.g. 7) for faster signals.
- Make oversold/overbought configurable (e.g. 25/75 or 20/80 for crypto).
- Consider a simple trend filter (e.g. price vs 50-period MA) to avoid counter-trend entries.

---

## 2. MACD Strategy (`strategies/macd.py`)

**Current:** 12/26/9, crossover on histogram zero-cross.

**Comparison:** Standard MACD. Implementation is correct.

**Recommendations:**

- Strength formula `abs(curr_hist) / price * 1000` is arbitrary; consider normalizing by ATR or recent volatility.
- Add optional confirmation (e.g. price above/below 200 MA for trend alignment).
- Consider histogram magnitude threshold to filter weak crossovers.

---

## 3. Bollinger Bands Strategy (`strategies/bollinger.py`)

**Current:** Period 20, 2 std dev; buy at lower band, sell at upper.

**Comparison:** Classic mean reversion. Implementation is correct.

**Recommendations:**

- Add volume confirmation (e.g. volume > average when touching bands).
- Consider “walking the band” — price can stay oversold/overbought in strong trends; a trend filter would help.
- Band width filter (you already handle zero width) — consider skipping when bands are very narrow (low volatility).

---

## 4. Compound Momentum Strategy (`strategies/compound_momentum.py`)

**Current:** Spike detection (1% move, 1.8x volume), breakout (0.5% threshold, 20-candle consolidation).

**Comparison:** Reasonable scalping logic. Parameters may be too permissive.

**Recommendations:**

- **Spike threshold:** 1% is low for crypto; consider 1.5–2% to reduce noise.
- **Volume surge:** 1.8x may let in weak moves; 2.0–2.5x could improve quality.
- **Breakout range:** 0.3–8% range is wide; consider 0.5–5% for cleaner setups.
- **RSI confirmation:** `rsi_bull_min`/`rsi_bear_max` at 50 is neutral; consider 45/55 for directional bias.

---

## 5. Swing Opportunity Strategy (`strategies/swing_opportunity.py`)

**Current:** 15% crash threshold, RSI < 20, 3x volume, near 200 MA.

**Comparison:** Very selective; design is sound for rare events.

**Recommendations:**

- Parameters are reasonable; main risk is catching “falling knives.”
- Consider requiring a small bounce (e.g. close > open on last candle) before entry.
- Cooldown of 60 candles is good; consider 90–120 for extreme crashes to avoid re-entry too soon.

---

## 6. Signal Generator Gating (`services/signal_generator.py`)

The hub’s `trending_momentum` and related strategies have been tightened (directional alignment, CEX confidence, max age). This is a positive change. Continue to:

- Monitor win rate and expectancy per strategy.
- Use analytics feedback to disable or down-weight underperformers.
- Keep max proposal age short (e.g. 2h for daily strategies).

---

## 7. Priority Recommendations


| Priority | Change                                                           | Impact                                  |
| -------- | ---------------------------------------------------------------- | --------------------------------------- |
| 1        | RSI: Add 7-period option, 25/75 or 20/80 thresholds              | Reduce false signals, better crypto fit |
| 2        | Compound Momentum: Raise spike threshold to 1.5%, volume to 2.0x | Fewer low-quality scalps                |
| 3        | MACD: Add histogram magnitude filter                             | Filter weak crossovers                  |
| 4        | Bollinger: Add volume confirmation                               | Higher-quality mean-reversion entries   |
| 5        | All: Add optional trend filter (price vs MA)                     | Avoid counter-trend trades              |


---

## 8. Testing Approach

1. **Backtest** each change on 6–12 months of data before deploying.
2. **Paper trade** new parameters for at least 1 week.
3. **A/B test** if possible: run old vs new params in parallel on different bots.
4. **Monitor** analytics engine output; disable strategies with negative expectancy.

---

*Generated as part of overnight codebase audit. Implement changes incrementally and validate each before production.*