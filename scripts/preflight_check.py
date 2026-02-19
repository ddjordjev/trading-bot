#!/usr/bin/env python3
"""Pre-flight check before going live.

Validates API keys, tests exchange connectivity, checks balance,
and verifies configuration sanity. Run this before your first live trade.

Usage:
    python scripts/preflight_check.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from config.settings import get_settings
from core.exchange.factory import create_exchange


async def run_checks() -> bool:
    settings = get_settings()
    passed = 0
    failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        status = "PASS" if ok else "FAIL"
        msg = f"  [{status}] {name}"
        if detail:
            msg += f" — {detail}"
        if ok:
            logger.info(msg)
            passed += 1
        else:
            logger.error(msg)
            failed += 1

    print("\n" + "=" * 60)
    print("  TRADING BOT — PRE-FLIGHT CHECK")
    print("=" * 60 + "\n")

    # 1. Config loaded
    check("Config loaded", True, f"mode={settings.trading_mode}, exchange={settings.exchange}")

    # 2. API keys present
    key_map = {
        "mexc": (settings.mexc_api_key, settings.mexc_api_secret),
        "binance": (settings.binance_api_key, settings.binance_api_secret),
        "bybit": (settings.bybit_api_key, settings.bybit_api_secret),
    }
    api_key, api_secret = key_map.get(settings.exchange, ("", ""))
    has_keys = bool(api_key and api_secret)
    check(f"API keys for {settings.exchange}", has_keys, "set" if has_keys else "MISSING — set in .env")

    if not has_keys:
        print("\nCannot continue without API keys. Set them in .env first.")
        return False

    # 3. Exchange connectivity
    exchange = create_exchange(settings)
    check(
        "Exchange capabilities", True, f"{exchange.name.upper()} supports: {', '.join(exchange.SUPPORTED_MARKET_TYPES)}"
    )
    check("Allowed market types", True, f"configured: {settings.allowed_market_types}")
    try:
        await exchange.connect()
        check("Exchange connectivity", True, f"connected to {exchange.name}")
    except Exception as e:
        check("Exchange connectivity", False, str(e))
        return False

    # 4. Balance check
    try:
        balance = await exchange.fetch_balance()
        raw_usdt = balance.get("USDT", 0)
        usdt = settings.cap_balance(raw_usdt)
        budget_note = f" (capped from ${raw_usdt:.2f})" if settings.session_budget > 0 and raw_usdt > usdt else ""
        check(
            "USDT balance",
            usdt >= settings.initial_risk_amount,
            f"${usdt:.2f} USDT{budget_note} (min ${settings.initial_risk_amount} needed)",
        )
        if settings.session_budget > 0:
            check("Session budget", True, f"${settings.session_budget:.2f}")
    except Exception as e:
        check("Balance fetch", False, str(e))

    # 5. Market data
    try:
        ticker = await exchange.fetch_ticker("BTC/USDT")
        check("Market data (BTC/USDT)", ticker.last > 0, f"last=${ticker.last:,.2f}")
    except Exception as e:
        check("Market data", False, str(e))

    # 6. Futures availability (skip if not allowed in config)
    if settings.futures_allowed:
        try:
            symbols = await exchange.get_available_symbols(market_type="futures")
            has_futures = len(symbols) > 0
            check(
                "Futures markets",
                has_futures,
                f"{len(symbols)} pairs" if has_futures else "NOT AVAILABLE — set ALLOWED_MARKET_TYPES=spot in .env",
            )
        except Exception as e:
            check(
                "Futures markets",
                False,
                f"{e} — if this exchange blocks futures for retail, set ALLOWED_MARKET_TYPES=spot in .env",
            )

        # 7. Leverage setting
        try:
            await exchange.set_leverage("BTC/USDT", settings.default_leverage)
            check("Leverage setting", True, f"{settings.default_leverage}x on BTC/USDT")
        except Exception as e:
            check("Leverage setting", False, f"Cannot set {settings.default_leverage}x — {e}")
    else:
        logger.info("  [SKIP] Futures checks — not in ALLOWED_MARKET_TYPES")
        logger.info("  [SKIP] Leverage checks — futures not enabled")

    # 8. Risk config sanity
    check("Daily loss limit", settings.max_daily_loss_pct <= 10, f"{settings.max_daily_loss_pct}%")
    check("Initial risk amount", settings.initial_risk_amount <= 500, f"${settings.initial_risk_amount}")
    check("Max notional cap", settings.max_notional_position >= 1000, f"${settings.max_notional_position:,.0f}")

    # 9. Trading mode
    mode_label = {
        "paper_local": "PAPER LOCAL (simulated, safe)",
        "paper_live": "PAPER LIVE (testnet orders, fake money)",
        "live": "LIVE (real money!)",
    }.get(settings.trading_mode, settings.trading_mode)
    check("Trading mode", True, mode_label)

    is_paper = settings.trading_mode.startswith("paper")
    if not is_paper:
        print("\n  ⚠  WARNING: You are about to trade with REAL MONEY.")
        print("  Make sure you've tested thoroughly in paper mode first.\n")

    # 10. Email notifications
    has_email = bool(settings.smtp_user and settings.notify_email)
    check(
        "Email notifications",
        has_email,
        f"→ {settings.notify_email}" if has_email else "NOT SET — you won't get alerts",
    )

    await exchange.disconnect()

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'=' * 60}\n")

    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(run_checks())
    sys.exit(0 if ok else 1)
