from __future__ import annotations


class DBError(Exception):
    """Base database adapter error."""


class DBOperationalError(DBError):
    """Database operation failed and may be retryable."""


class DBIntegrityError(DBError):
    """Database integrity constraint violation."""
