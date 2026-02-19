from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from loguru import logger

from strategies.base import BaseStrategy

CUSTOM_DIR = Path(__file__).resolve().parent.parent / "custom_strategies"


def load_custom_strategies() -> dict[str, type[BaseStrategy]]:
    """Scan custom_strategies/ for Python files and load strategy classes.

    Each file is expected to contain at least one class that extends
    BaseStrategy. Files starting with '_' are skipped (e.g. _example.py).
    """
    strategies: dict[str, type[BaseStrategy]] = {}

    if not CUSTOM_DIR.is_dir():
        return strategies

    for path in sorted(CUSTOM_DIR.glob("*.py")):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue

        module_name = f"custom_strategies.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, BaseStrategy) and attr is not BaseStrategy:
                    instance = attr.__new__(attr)
                    try:
                        name = attr.name.fget(instance)  # type: ignore[union-attr]
                    except Exception:
                        name = attr_name.lower()
                    strategies[name] = attr
                    logger.info("Loaded custom strategy '{}' from {}", name, path.name)

        except Exception as e:
            logger.warning("Failed to load custom strategy from {}: {}", path.name, e)

    return strategies
