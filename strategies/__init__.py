from strategies.base import BaseStrategy
from strategies.rsi import RSIStrategy
from strategies.macd import MACDStrategy
from strategies.bollinger import BollingerStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.grid import GridStrategy
from strategies.market_open_volatility import MarketOpenVolatilityStrategy
from strategies.compound_momentum import CompoundMomentumStrategy
from strategies.swing_opportunity import SwingOpportunityStrategy

BUILTIN_STRATEGIES: dict[str, type[BaseStrategy]] = {
    "rsi": RSIStrategy,
    "macd": MACDStrategy,
    "bollinger": BollingerStrategy,
    "mean_reversion": MeanReversionStrategy,
    "grid": GridStrategy,
    "market_open_volatility": MarketOpenVolatilityStrategy,
    "compound_momentum": CompoundMomentumStrategy,
    "swing_opportunity": SwingOpportunityStrategy,
}

__all__ = [
    "BaseStrategy",
    "RSIStrategy",
    "MACDStrategy",
    "BollingerStrategy",
    "MeanReversionStrategy",
    "GridStrategy",
    "MarketOpenVolatilityStrategy",
    "CompoundMomentumStrategy",
    "SwingOpportunityStrategy",
    "BUILTIN_STRATEGIES",
]
