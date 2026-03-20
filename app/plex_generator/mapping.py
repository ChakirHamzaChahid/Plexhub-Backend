from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("plexhub.plex_generator.mapping")

MAPPING_FILENAME = ".plex_mapping.json"


class MappingEntry:
    __slots__ = ("path", "stream_url", "updated_at")

    def __init__(self, path: str, stream_url: str, updated_at: str):
        self.path = path
        self.stream_url = stream_url
        self.updated_at = updated_at

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "stream_url": self.stream_url,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MappingEntry":
        return cls(
            path=data["path"],
            stream_url=data["stream_url"],
            updated_at=data.get("updated_at", ""),
        )


class MappingStore:
    """JSON-backed mapping from source_id to local file path + stream URL."""

    def __init__(self, base_dir: Path):
        self._file = base_dir / MAPPING_FILENAME
        self._data: dict[str, MappingEntry] = {}

    def load(self) -> None:
        if not self._file.exists():
            self._data = {}
            return
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
            self._data = {
                k: MappingEntry.from_dict(v) for k, v in raw.items()
            }
            logger.info(f"Loaded mapping with {len(self._data)} entries")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Corrupted mapping file, starting fresh: {e}")
            self._data = {}

    def save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        content = json.dumps(
            {k: v.to_dict() for k, v in self._data.items()},
            indent=2,
            ensure_ascii=False,
        )
        tmp.write_text(content, encoding="utf-8")
        # Atomic rename (on Windows, need to remove target first)
        if os.name == "nt" and self._file.exists():
            self._file.unlink()
        tmp.rename(self._file)
        logger.info(f"Saved mapping with {len(self._data)} entries")

    def get(self, source_id: str) -> MappingEntry | None:
        return self._data.get(source_id)

    def set(self, source_id: str, path: str, stream_url: str) -> None:
        self._data[source_id] = MappingEntry(
            path=path,
            stream_url=stream_url,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def remove(self, source_id: str) -> MappingEntry | None:
        return self._data.pop(source_id, None)

    def all_source_ids(self) -> set[str]:
        return set(self._data.keys())

    def __len__(self) -> int:
        return len(self._data)
