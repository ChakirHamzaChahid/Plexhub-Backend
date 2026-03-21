import logging
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

logger = logging.getLogger("plexhub.plex_generator.storage")


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
        self._redirect_client: httpx.Client | None = None

    def _resolve(self, rel_path: str) -> Path:
        return self.base_dir / rel_path

    def _get_redirect_client(self) -> httpx.Client:
        if self._redirect_client is None or self._redirect_client.is_closed:
            self._redirect_client = httpx.Client(timeout=10.0, follow_redirects=False)
        return self._redirect_client

    def _resolve_redirect(self, url: str) -> str:
        """Follow a single 3xx redirect and return the final URL."""
        try:
            client = self._get_redirect_client()
            resp = client.head(url)
            if resp.status_code in (301, 302, 307, 308):
                location = resp.headers.get("location", url)
                logger.debug(f"Resolved redirect: {url} -> {location}")
                return location
        except Exception as e:
            logger.debug(f"Failed to resolve redirect for {url}: {e}")
        return url

    def write_strm(self, rel_path: str, url: str) -> None:
        full = self._resolve(rel_path)
        full.parent.mkdir(parents=True, exist_ok=True)
        resolved = self._resolve_redirect(url)
        full.write_text(resolved.strip() + "\n", encoding="utf-8")

    def write_file(self, rel_path: str, content: str) -> None:
        full = self._resolve(rel_path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    def download_image(self, rel_path: str, image_url: str) -> bool:
        full = self._resolve(rel_path)
        full.parent.mkdir(parents=True, exist_ok=True)
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                resp = client.get(image_url)
                resp.raise_for_status()
                full.write_bytes(resp.content)
            return True
        except Exception as e:
            logger.debug(f"Failed to download image {image_url}: {e}")
            return False

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
