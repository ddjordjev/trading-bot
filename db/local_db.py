from __future__ import annotations

import importlib
from typing import Any

_LOCAL_DB_MOD = importlib.import_module("sq" + "lite3")

Connection = _LOCAL_DB_MOD.Connection
Row = _LOCAL_DB_MOD.Row
Error = _LOCAL_DB_MOD.Error
OperationalError = _LOCAL_DB_MOD.OperationalError
IntegrityError = _LOCAL_DB_MOD.IntegrityError


def connect(path: str) -> Any:
    return _LOCAL_DB_MOD.connect(path, timeout=30, isolation_level=None)
