import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

logger = logging.getLogger("plexhub.plex_generator.storage")

# Shared thread pool for image downloads (avoids creating a pool per image)
_image_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="img-dl")
# Shared httpx client for image downloads (connection reuse across threads)
_image_http_client: httpx.Client | None = None


def _get_image_client() -> httpx.Client:
    global _image_http_client
    if _image_http_client is None or _image_http_client.is_closed:
        _image_http_client = httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=8),
        )
    return _image_http_client


class LibraryStorage(ABC):
    """Abstract storage layer for Plex library files."""

    @abstractmethod
    def write_strm(self, rel_path: str, url: str) -> None: ...

    @abstractmethod
    def write_file(self, rel_path: str, content: str) -> None: ...

    @abstractmethod
    def download_image(self, rel_path: str, image_url: str) -> bool: ...

    @abstractmethod
    def delete_file(self, rel_path: str) -> None: ...

    @abstractmethod
    def read_strm(self, rel_path: str) -> str | None: ...

    @abstractmethod
    def cleanup_empty_dirs(self, rel_path: str) -> None: ...


class LocalStorage(LibraryStorage):
    """Writes Plex library files to the local filesystem."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def _resolve(self, rel_path: str) -> Path:
        return self.base_dir / rel_path

    def write_strm(self, rel_path: str, url: str) -> None:
        full = self._resolve(rel_path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(url.strip() + "\n", encoding="utf-8")

    def write_file(self, rel_path: str, content: str) -> None:
        full = self._resolve(rel_path)
        if full.exists():
            return  # Preserve existing file (e.g. enriched by Tiny Media Manager)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    def download_image(self, rel_path: str, image_url: str) -> bool:
        full = self._resolve(rel_path)
        if full.exists():
            return True  # Preserve existing image (e.g. enriched by Tiny Media Manager)
        full.parent.mkdir(parents=True, exist_ok=True)
        try:
            future = _image_pool.submit(self._download_sync, full, image_url)
            return future.result(timeout=20.0)
        except Exception as e:
            logger.debug(f"Failed to download image {image_url}: {e}")
            return False

    @staticmethod
    def _download_sync(full: Path, image_url: str) -> bool:
        client = _get_image_client()
        resp = client.get(image_url)
        resp.raise_for_status()
        full.write_bytes(resp.content)
        return True

    def delete_file(self, rel_path: str) -> None:
        full = self._resolve(rel_path)
        if full.exists():
            full.unlink()

    def read_strm(self, rel_path: str) -> str | None:
        full = self._resolve(rel_path)
        if not full.exists():
            return None
        return full.read_text(encoding="utf-8").strip()

    def cleanup_empty_dirs(self, rel_path: str) -> None:
        """Remove empty parent directories up to Films/ or Series/."""
        full = self._resolve(rel_path)
        current = full.parent
        while current != self.base_dir:
            try:
                if current.exists() and not any(current.iterdir()):
                    current.rmdir()
                    current = current.parent
                else:
                    break
            except OSError:
                break


class DryRunStorage(LibraryStorage):
    """No-op storage that logs operations without writing to disk."""

    def write_strm(self, rel_path: str, url: str) -> None:
        logger.info(f"[DRY-RUN] write_strm: {rel_path}")

    def write_file(self, rel_path: str, content: str) -> None:
        logger.info(f"[DRY-RUN] write_file: {rel_path}")

    def download_image(self, rel_path: str, image_url: str) -> bool:
        logger.info(f"[DRY-RUN] download_image: {rel_path}")
        return True

    def delete_file(self, rel_path: str) -> None:
        logger.info(f"[DRY-RUN] delete_file: {rel_path}")

    def read_strm(self, rel_path: str) -> str | None:
        return None

    def cleanup_empty_dirs(self, rel_path: str) -> None:
        pass
