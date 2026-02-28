# Trading Bot

Modular crypto trading bot with volatility exploitation, supporting BYBIT (extensible to other exchanges).

## Features

- **Multi-exchange support** via abstract exchange layer (BYBIT implemented, add more by subclassing `BaseExchange`)
- **Spot & Futures** trading with configurable leverage (default 10x)
- **Paper trading** mode using real market data with simulated orders
- **Built-in strategies**: RSI, MACD, Bollinger Bands, Mean Reversion, Grid, Market Open Volatility
- **Custom strategy support** via `BaseStrategy` subclass
- **Volatility/spike detection** engine with configurable thresholds
- **Market open window** awareness (US and Asia session opens)
- **News monitoring** via RSS feeds with sentiment analysis and spike correlation
- **Email notifications** (liquidation alerts always on, plus configurable alerts)
- **Risk management**: position sizing, daily loss limits, stop-loss/take-profit, liquidation detection

## Quick Start (Local)

```bash
# 1. Clone and enter the project
cd trading-bot

# 2. Run the setup script
chmod +x scripts/run-local.sh
./scripts/run-local.sh

# 3. Edit .env with your BYBIT API keys
# 4. Run again
./scripts/run-local.sh
```

## Quick Start (Docker / DigitalOcean)

```bash
# Local Docker
docker compose up -d --build

# DigitalOcean
chmod +x scripts/deploy-digitalocean.sh
./scripts/deploy-digitalocean.sh
```

## Configuration

All config is via `.env` file (copy from `.env.example`). Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `EXCHANGE` | `bybit` | Exchange to use |
| `DEFAULT_LEVERAGE` | `10` | Default futures leverage |
| `MAX_POSITION_SIZE_PCT` | `10` | Max % of balance per position |
| `MAX_DAILY_LOSS_PCT` | `5` | Stop trading after this daily loss |
| `SPIKE_THRESHOLD_PCT` | `3.0` | % move to trigger spike alert |
| `NEWS_ENABLED` | `false` | Enable RSS news monitoring |

## Adding a Custom Strategy

```python
from strategies.base import BaseStrategy
from core.models import Candle, Ticker, Signal, SignalAction

class MyStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "my_strategy"

    def analyze(self, candles, ticker=None):
        # Your logic here
        # Return a Signal to trade, or None to do nothing
        return Signal(
            symbol=self.symbol,
            action=SignalAction.BUY,
            strength=0.8,
            strategy=self.name,
            reason="my custom reason",
            suggested_price=candles[-1].close,
            market_type=self.market_type,
            leverage=self.leverage,
            quick_trade=True,          # for fast in-and-out
            max_hold_minutes=15,       # auto-close after 15 min
        )
```

Register it in `bot.py`:

```python
bot.add_custom_strategy(MyStrategy("BTC/USDT", market_type="futures", leverage=10))
```

## Project Structure

```
trading-bot/
├── bot.py                  # Main entry point
├── config/                 # Settings and .env loading
├── core/
│   ├── exchange/           # Exchange abstraction + BYBIT impl
│   ├── models/             # Candle, Order, Position, Signal
│   ├── orders/             # Order execution and management
│   └── risk/               # Risk management
├── strategies/             # Trading strategies (add yours here)
├── volatility/             # Spike and volatility detection
├── notifications/          # Email alert system
├── news/                   # RSS news monitoring
├── scripts/                # Run and deploy scripts
├── Dockerfile
└── docker-compose.yml
```
