# db_intel_scripts — Deep audit report

**Scope:** db/store.py, db/models.py, intel/*.py, shared/state.py, shared/models.py, scripts/preflight_check.py, scripts/run_session.sh, notifications/notifier.py, core/market_schedule.py

**Date:** 2026-02-20

---

## 1. BUG: TradingView client never polls — cache only filled on explicit `analyze()`

**File:** `intel/tradingview.py`  
**Lines:** 130–137 (start), 124–127 (__init__)

**What’s wrong:**  
`TradingViewClient.start()` only sets `_running = True` and logs. It does **not** start a background task (no `asyncio.create_task(self._poll_loop())`). There is no `_poll_loop` method. So `poll_interval` is stored but never used, and the cache is only populated when some caller explicitly invokes `analyze()` or `analyze_multi()`. If nothing calls those, `consensus()` and `signal_boost()` always see empty cache and return `"no_data"` / `1.0`.

**Code (excerpt):**
```python
async def start(self) -> None:
    self._running = True
    logger.info(...)  # no create_task(_poll_loop())
```

**Fix:**  
Either:

- Add a `_poll_loop` that periodically calls `analyze()` for configured symbols/intervals and start it in `start()` with `asyncio.create_task(self._poll_loop())`, using `self.poll_interval` and `self.intervals`, or  
- Remove `poll_interval` and document that TV is on-demand only, and ensure the monitor/bot explicitly refreshes TV (e.g. for symbols in the queue) so consensus isn’t stuck at `"no_data"`.

---

## 2. BUG: CoinMarketCap “recently added” assumes `quotes` is a list — crash if API returns dict

**File:** `intel/coinmarketcap.py`  
**Lines:** 261–265

**What’s wrong:**  
`_fetch_recently_added()` does `quotes = item.get("quotes", [{}])` and then `q = quotes[0] if quotes else {}`. If the CMC API returns `quotes` as a **dict** (e.g. `{"USD": {"price": ..., "volume24h": ...}}`), then `quotes[0]` raises `TypeError` (dicts don’t support integer indexing), and the whole fetch fails for that endpoint.

**Code:**
```python
quotes = item.get("quotes", [{}])
q = quotes[0] if quotes else {}
```

**Fix:**  
Support both list and dict:

```python
quotes = item.get("quotes") or {}
if isinstance(quotes, dict):
    q = quotes.get("USD", {})
else:
    q = quotes[0] if quotes else {}
```

Then use `q` for `price`, `volume24h`, `marketCap`, etc., as you do now.

---

## 3. BUG: Whale sentiment funding-rate threshold likely wrong unit (5% vs 0.05%)

**File:** `intel/whale_sentiment.py`  
**Lines:** 38–44, 186–191, 244

**What’s wrong:**  
`is_overleveraged_longs` uses `self.funding_rate > 0.05`. In crypto, funding is usually reported as a decimal (e.g. 0.0001 = 0.01%). So 0.05 would mean **5%**, which is extreme and rare. The docstring says “Extreme positive funding rate (>0.05%)” — i.e. 0.05% — which in decimal is 0.0005. If the CoinGlass API returns a decimal rate, the current check almost never triggers; the intended behavior is almost certainly “> 0.05%” i.e. `> 0.0005`.

**Code:**
```python
@property
def is_overleveraged_longs(self) -> bool:
    return self.funding_rate > 0.05 and self.long_short_ratio > 1.5
```

**Fix:**  
- If the API returns rate in **decimal** (e.g. 0.0001 for 0.01%): use `0.0005` (0.05%) instead of `0.05`, and for shorts use `-0.0005` instead of `-0.05`.  
- If the API returns rate in **percent** (e.g. 0.01 for 0.01%): keep 0.05 but then the log `data.funding_rate * 100` would be wrong (would show 500% for 0.05). So clarify API unit and either:  
  - treat as decimal and use `0.0005` / `-0.0005`, and keep `* 100` in logs, or  
  - treat as percent and use `0.05` / `-0.05` and do not multiply by 100 in logs.

---

## 4. DEFENSIVE: DB `get_strategy_stats` can return `None` for aggregate columns

**File:** `db/store.py`  
**Lines:** 163–181

**What’s wrong:**  
When there are **no** trades matching the filter, the DB still returns one aggregate row; `AVG(...)` and `AVG(hold_minutes)` are `NULL`. So `dict(row)` can contain `None` for `avg_win`, `avg_loss`, `avg_hold`. The main consumer (`analytics/engine.py`) does `stats["avg_win"] or 0` etc., so it’s safe there, but the return type is `dict[str, Any]` and any other caller or JSON serialization could see `None` and break (e.g. arithmetic or `.toFixed()`).

**Fix:**  
Either normalize in the store so aggregates are never `None` (e.g. `avg_win = row["avg_win"] if row["avg_win"] is not None else 0.0` and same for `avg_loss`, `avg_hold` before building the dict), or document that these keys may be `None` when there are no matching trades and that callers must coerce.

---

## 5. VERIFIED NOT BUGS (for reference)

- **Macro calendar country filter:** The feed at `nfs.faireconomy.media/ff_calendar_thisweek.json` uses `"country":"USD"` for US events. Filtering with `country != "USD"` would drop them; the code keeps `country == "USD"`, so US events are correctly included.  
- **db package:** `db/__init__.py` exports `TradeDB`; `run_session.sh` and bot use `from db import TradeDB` correctly.  
- **Shared state / TradeQueue:** Locking and read-modify-write in `apply_trade_queue_updates` are correct; Pydantic model mutation is in place and then written back.  
- **Preflight:** Balance-fetch failure increments `failed` and the script returns `failed == 0`, so the run correctly fails.  
- **Notifier:** `enabled_types` comes from `settings.notification_list` (list of strings), so `", ".join(self.enabled_types)` is valid.

---

## Summary

| # | Severity   | File                  | Issue |
|---|------------|------------------------|-------|
| 1 | High       | intel/tradingview.py   | No TV background poll; cache often empty; consensus/signal_boost ineffective. |
| 2 | High       | intel/coinmarketcap.py | `quotes[0]` assumes list; TypeError if API returns dict. |
| 3 | Medium     | intel/whale_sentiment.py | Funding threshold 0.05 likely wrong unit (should be 0.0005 for 0.05%). |
| 4 | Low        | db/store.py           | get_strategy_stats can return None for avg_win/avg_loss/avg_hold; caller handles it but contract is unclear. |

No logic errors, off-by-ones, or stale references were found in the other scanned files within the db_intel_scripts group.
