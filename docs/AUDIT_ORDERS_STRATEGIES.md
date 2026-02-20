# Orders & Strategies Audit Report

**Scope:** `core/orders/*`, `strategies/*` (orders_strategies group)  
**Focus:** Logic errors, state bugs, type/None issues, missing error handling, stale references, config, math, signals, order management.

---

## 1. **core/orders/manager.py** — Position uses requested amount instead of filled amount

**Location:** ~184–192  
**What’s wrong:** After a filled order, the `Position` passed to `trailing.register()` is built with `amount=amount` (the requested size) instead of `amount=order.filled`. On partial fills or rounding, the tracked size is wrong.

**Code:**
```python
pos = Position(
    symbol=signal.symbol,
    side=side,
    amount=amount,  # <-- should be order.filled
    entry_price=order.average_price,
    ...
)
```

**Fix:** Use the filled size and avoid division-by-zero when logging/tracking:
```python
pos = Position(
    symbol=signal.symbol,
    side=side,
    amount=order.filled,
    entry_price=order.average_price,
    current_price=order.average_price,
    leverage=actual_leverage,
    market_type=market_type.value,
)
```

---

## 2. **core/orders/manager.py** — Hedge and wick scalp closes never record PnL

**Location:** `_close_sub_position` (~506–521), `_close_sub_position_wick` (~524–544), and expired wick close in `try_wick_scalps` (~451–462).  
**What’s wrong:** Full position close and partial take both call `self.risk.record_pnl(...)`. Closing a hedge or a wick scalp (stop hit or expired) does not. Daily PnL and risk limits are therefore wrong when hedges/wick scalps are closed.

**Fix:** After a filled close order in both flows, compute realized PnL and call `self.risk.record_pnl(pnl)`.

- **Hedge:** e.g. for a short hedge: `pnl = (pair.hedge_entry - order.average_price) * (pair.hedge_size / pair.hedge_entry)` when `pair.hedge_entry > 0`; for long hedge invert. Then `self.risk.record_pnl(pnl)`.
- **Wick scalp:** same idea using scalp entry price and closed amount; then `self.risk.record_pnl(pnl)`.
- **Expired wick close in try_wick_scalps:** when the close order is filled, compute PnL from scalp entry vs `close_order.average_price` and amount, then `self.risk.record_pnl(pnl)`.

---

## 3. **core/orders/manager.py** — Full close only uses first position when multiple exist

**Location:** `_close_position` ~679–682  
**What’s wrong:** `positions = await self.exchange.fetch_positions(signal.symbol)` and then `pos = positions[0]` and a single close for `pos.amount`. If the exchange returns more than one position per symbol (e.g. separate long/short or legs), only the first is closed; others stay open.

**Code:**
```python
positions = await self.exchange.fetch_positions(signal.symbol)
...
pos = positions[0]
...
order = await self.exchange.place_order(..., amount=pos.amount, ...)
```

**Fix:** Either document that the exchange must return a single net position per symbol and assert `len(positions) == 1`, or close all: loop over `positions` and place a close for each, or aggregate to a net close size and close once, depending on exchange semantics.

---

## 4. **core/orders/trailing.py** — Hedge/wick stops never update if main position is gone

**Location:** `TrailingStopManager.update_all` ~226–237  
**What’s wrong:** `price_map` is built only from `positions`. Stops are keyed by symbol or `symbol:hedge` / `symbol:wick`, but the price used is `price_map.get(ts.symbol)`. If the main position for that symbol is gone (e.g. closed externally or liquidated), there is no entry for `ts.symbol`, so `price` is `None` and the stop is skipped. Hedge/wick stops then never trigger, and sub-positions can be left open without our logic ever closing them.

**Fix:** Either:
- Ensure every symbol that has any stop (including `:hedge` / `:wick`) gets a price, e.g. from a ticker/last-price source when it’s missing from `positions`, or
- When building `price_map`, if a key is missing but we have `:hedge` or `:wick` stops for that symbol, treat “no position” as “close sub-positions and remove those stops” and close the hedge/wick explicitly (using stored size/entry) so they are not orphaned.

---

## 5. **strategies/compound_momentum.py** — Spike volume check ignores param

**Location:** ~117–118  
**What’s wrong:** Breakout uses `self.volume_surge_mult` (default 1.8). Spike detection uses a hardcoded `1.5` for the volume ratio, so config is inconsistent and spike entries can fire with lower volume than the configured surge multiplier.

**Code:**
```python
if vol_ratio < 1.5:  # should use self.volume_surge_mult
    return None
```

**Fix:** Use the same param for consistency, e.g. `if vol_ratio < self.volume_surge_mult: return None`, or introduce a dedicated `spike_volume_mult` and use it here.

---

## Summary

| # | File | Severity | Issue |
|---|------|----------|--------|
| 1 | core/orders/manager.py | Low | Position built with requested `amount` instead of `order.filled` |
| 2 | core/orders/manager.py | High | Hedge and wick scalp closes do not call `risk.record_pnl()` |
| 3 | core/orders/manager.py | Medium | Full close uses only `positions[0]`; multiple positions per symbol not handled |
| 4 | core/orders/trailing.py | High | Hedge/wick stops never get price when main position is missing; sub-positions can be orphaned |
| 5 | strategies/compound_momentum.py | Low | Spike volume threshold hardcoded 1.5 instead of using `volume_surge_mult` |

No other **concrete** bugs (wrong math, inverted conditions, missing None checks, stale refs, or signal/order tracking errors) were found in the audited files. Redundant `if order.status == OrderStatus.FILLED` and unused `stop_price` in hedge params are minor/cleanup only.
