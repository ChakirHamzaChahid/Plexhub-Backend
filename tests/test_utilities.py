"""Tests for utilities: EPG base64, background tasks, config, mapping purge."""
import asyncio

import pytest

from app.api.live import _try_base64_decode
from app.utils.tasks import (
    create_background_task,
    cancel_task_by_name,
    cancel_all_background_tasks,
    _background_tasks,
)
from app.config import _safe_int
from app.plex_generator.mapping import MappingStore


# ─── Base64 EPG Decode ──────────────────────────────────────────


class TestBase64Decode:
    def test_actual_base64_text(self):
        import base64
        encoded = base64.b64encode("Programme TV du soir".encode()).decode()
        assert _try_base64_decode(encoded) == "Programme TV du soir"

    def test_normal_text_preserved(self):
        """Normal text that is NOT base64 should be returned as-is."""
        assert _try_base64_decode("Evening News") == "Evening News"

    def test_text_that_looks_like_base64_but_decodes_to_garbage(self):
        """'News' is valid base64 but decodes to binary. Should be kept as-is."""
        # 'News' base64-decodes to bytes \x36\xeb\x2c which contains control chars
        result = _try_base64_decode("News")
        # Should return original since decoded contains non-printable chars
        assert result == "News"

    def test_empty_string(self):
        assert _try_base64_decode("") == ""

    def test_none_like(self):
        assert _try_base64_decode("") == ""

    def test_unicode_base64(self):
        import base64
        original = "Les Misérables"
        encoded = base64.b64encode(original.encode("utf-8")).decode()
        assert _try_base64_decode(encoded) == original

    def test_multiline_base64(self):
        import base64
        original = "Line 1\nLine 2\nLine 3"
        encoded = base64.b64encode(original.encode()).decode()
        assert _try_base64_decode(encoded) == original


# ─── Config Safe Int ────────────────────────────────────────────


class TestSafeInt:
    def test_valid_int(self):
        import os
        os.environ["_TEST_SAFE_INT"] = "42"
        assert _safe_int("_TEST_SAFE_INT", 0) == 42
        del os.environ["_TEST_SAFE_INT"]

    def test_invalid_int_uses_default(self):
        import os
        os.environ["_TEST_SAFE_INT"] = "not_a_number"
        assert _safe_int("_TEST_SAFE_INT", 99) == 99
        del os.environ["_TEST_SAFE_INT"]

    def test_empty_string_uses_default(self):
        import os
        os.environ["_TEST_SAFE_INT"] = ""
        assert _safe_int("_TEST_SAFE_INT", 7) == 7
        del os.environ["_TEST_SAFE_INT"]

    def test_float_string_uses_default(self):
        import os
        os.environ["_TEST_SAFE_INT"] = "6.5"
        assert _safe_int("_TEST_SAFE_INT", 6) == 6
        del os.environ["_TEST_SAFE_INT"]

    def test_missing_env_uses_default(self):
        assert _safe_int("_DEFINITELY_NOT_SET_XYZ_123", 100) == 100


# ─── Background Tasks ──────────────────────────────────────────


class TestBackgroundTasks:
    def test_create_and_track(self):
        async def _test():
            async def dummy():
                await asyncio.sleep(0.01)

            task = create_background_task(dummy(), name="test_task")
            assert task in _background_tasks
            await task
            # After completion, callback removes it
            await asyncio.sleep(0.05)  # Let callback fire
            assert task not in _background_tasks

        asyncio.run(_test())

    def test_cancel_by_name(self):
        async def _test():
            async def long_task():
                await asyncio.sleep(100)

            task = create_background_task(long_task(), name="cancel_me")
            assert cancel_task_by_name("cancel_me") is True
            assert cancel_task_by_name("nonexistent") is False
            # Cleanup
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_test())

    def test_cancel_all_with_timeout(self):
        async def _test():
            async def long_task():
                await asyncio.sleep(100)

            create_background_task(long_task(), name="t1")
            create_background_task(long_task(), name="t2")

            await cancel_all_background_tasks(timeout=2.0)
            assert len(_background_tasks) == 0

        asyncio.run(_test())


# ─── Mapping Purge ──────────────────────────────────────────────


class TestMappingPurge:
    def test_purge_stale(self, tmp_path):
        store = MappingStore(tmp_path)
        store.load()
        store.set("keep_1", "path1", "url1")
        store.set("keep_2", "path2", "url2")
        store.set("stale_1", "path3", "url3")
        store.set("stale_2", "path4", "url4")

        removed = store.purge_stale({"keep_1", "keep_2"})
        assert removed == 2
        assert store.get("keep_1") is not None
        assert store.get("keep_2") is not None
        assert store.get("stale_1") is None
        assert store.get("stale_2") is None
        assert len(store) == 2

    def test_purge_nothing(self, tmp_path):
        store = MappingStore(tmp_path)
        store.load()
        store.set("a", "p1", "u1")
        removed = store.purge_stale({"a"})
        assert removed == 0
        assert len(store) == 1
