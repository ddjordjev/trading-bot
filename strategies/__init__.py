from strategies.base import BaseStrategy
from strategies.bollinger import BollingerStrategy
from strategies.compound_momentum import CompoundMomentumStrategy
from strategies.custom_loader import load_custom_strategies
from strategies.grid import GridStrategy
from strategies.macd import MACDStrategy
from strategies.market_open_volatility import MarketOpenVolatilityStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.rsi import RSIStrategy
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


def get_all_strategies() -> dict[str, type[BaseStrategy]]:
    """Built-in strategies merged with custom strategies from custom_strategies/."""
    return {**BUILTIN_STRATEGIES, **load_custom_strategies()}


__all__ = [
    "BUILTIN_STRATEGIES",
    "BaseStrategy",
    "BollingerStrategy",
    "CompoundMomentumStrategy",
    "GridStrategy",
    "MACDStrategy",
    "MarketOpenVolatilityStrategy",
    "MeanReversionStrategy",
    "RSIStrategy",
    "SwingOpportunityStrategy",
    "get_all_strategies",
    "load_custom_strategies",
]
