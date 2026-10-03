"""Log capture that survives `propagate=False` on the "plexhub" logger.

`caplog` attaches to the ROOT logger; once `app.main` has been imported (any
test using `api_client`, or the lifespan), the "plexhub" logger stops
propagating and `caplog` silently sees nothing -- which turns an assertion like
`SECRET not in caplog.text` into a vacuous pass. This puts a handler directly
on the named logger (same fix as tests/test_backup_db.py, tests/test_dav_relay.py).
"""
from __future__ import annotations

import logging


class Records:
    def __init__(self, name: str = "plexhub", level: int = logging.DEBUG):
        self._logger = logging.getLogger(name)
        self._level = level
        self.records: list[logging.LogRecord] = []

    def __enter__(self) -> "Records":
        outer = self

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                outer.records.append(record)

        self._handler = _Handler(level=self._level)
        self._old_level = self._logger.level
        self._logger.setLevel(self._level)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc) -> bool:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._old_level)
        return False

    @property
    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)
