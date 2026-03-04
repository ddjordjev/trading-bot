from __future__ import annotations

from pathlib import Path
from typing import Protocol

from config.settings import get_settings
from db.hub_store import HubDB
from db.hub_store_postgres import PostgresHubDB


class HubRepository(Protocol):
    def connect(self) -> None: ...
    def close(self) -> None: ...


def make_hub_repository(path: Path | None = None) -> HubDB | PostgresHubDB:
    settings = get_settings()
    backend = str(settings.hub_db_backend or "sqlite").strip().lower()
    if backend == "postgres":
        return PostgresHubDB(dsn=settings.hub_postgres_dsn)
    return HubDB(path=path or Path("data/hub.db"))
