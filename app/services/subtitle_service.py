"""Subtitle parsing, translation, and re-serialization service.

Supports SRT and WebVTT formats. Translation is performed via the local Ollama
LLM (gemma4 by default), chunked and run concurrently.

Public API (imported by WP4 endpoint):
  SubtitleFormatError      — malformed or unrecognized input → HTTP 422
  SubtitleTooLargeError    — exceeds size or cue limits       → HTTP 413
  SubtitleTranslation      — result dataclass
  translate_subtitles()    — main entry point

Newline-placeholder scheme:
  Intra-cue line breaks are replaced by the sentinel " ⏎ " before being sent
  to the LLM (unlikely to appear in subtitle text) and restored afterwards.
  The numbered-list format sent to the model is:

      Translate each numbered line below to <target_lang>.
      Return EXACTLY <n> numbered lines in the same order.
      ...
      1. first line of cue / second line of cue
      2. another cue

Alignment fallback:
  If the model returns a different count than expected the chunk is retried
  once. If it still mismatches, every cue in that chunk keeps its original
  text (no cue is ever dropped or shifted).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from sqlalchemy import delete

from app.config import settings
from app.services import ollama_service

logger = logging.getLogger("plexhub.subtitle")

# Sentinel used to encode intra-cue newlines before sending to the LLM.
# Chosen to be visually clear, not present in ordinary subtitle text, and
# never confused with the LLM's "1. …" numbered list syntax.
# Assumption: " ⏎ " does not occur in source cue text. If it does, the
# restore step (_restore_newlines) is best-effort and may introduce an
# extra newline, but no cue will be dropped.
_NEWLINE_SENTINEL = " ⏎ "


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------

class SubtitleFormatError(Exception):
    """Malformed or unrecognized subtitle input.  Caller maps to HTTP 422."""


class SubtitleTooLargeError(Exception):
    """Content exceeds configured size or cue limits.  Caller maps to HTTP 413."""


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

@dataclass
class SubtitleTranslation:
    translated_content: str
    fmt: str        # "srt" or "vtt"
    cue_count: int
    model: str


# ---------------------------------------------------------------------------
# Internal block types
# ---------------------------------------------------------------------------

@dataclass
class _Cue:
    """A single translatable subtitle cue."""
    index: int                   # position in the block list (0-based)
    header: str                  # e.g. "1" (SRT index) or cue id / timecode line (VTT)
    timecode: str                # "00:00:01,000 --> 00:00:02,000" — preserved verbatim
    cue_settings: str            # VTT only: settings after the timecode (may be "")
    text: str                    # raw text with original newlines
    fmt: str                     # "srt" or "vtt"


@dataclass
class _Passthrough:
    """A block that must be re-emitted verbatim (VTT NOTE/STYLE/header, blank separators)."""
    index: int
    content: str                 # exactly as it appeared in the source


# Block = cue or passthrough
_Block = _Cue | _Passthrough


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_format(content: str) -> str:
    """Return 'vtt' or 'srt'.  BOM-tolerant.

    'vtt' when the stripped content starts with WEBVTT; 'srt' otherwise.
    Raises SubtitleFormatError for empty input.
    """
    stripped = content.lstrip("﻿").lstrip()
    if not stripped:
        raise SubtitleFormatError("Empty subtitle content")
    if stripped.startswith("WEBVTT"):
        return "vtt"
    return "srt"


# ---------------------------------------------------------------------------
# SRT parser
# ---------------------------------------------------------------------------

# SRT timecode: HH:MM:SS,mmm --> HH:MM:SS,mmm  (commas, no cue settings)
_SRT_TIMECODE_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})"   # start
    r"\s*-->\s*"
    r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})"   # end
    r"(.*)"                                   # trailing garbage (rare)
)


def _parse_srt(content: str) -> list[_Block]:
    """Parse SRT content into a list of _Cue blocks.

    Blocks are separated by one or more blank lines.  Each block must have:
      line 0 — integer index
      line 1 — timecode line matching _SRT_TIMECODE_RE
      lines 2+ — cue text
    Blocks that don't match are silently dropped (malformed partial blocks).
    """
    # Normalize CRLF → LF
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    raw_blocks = re.split(r"\n{2,}", content.strip())
    blocks: list[_Block] = []
    for idx, raw in enumerate(raw_blocks):
        lines = raw.strip().splitlines()
        if len(lines) < 3:
            continue
        # Line 0: index (must be a digit sequence)
        index_line = lines[0].strip()
        if not index_line.isdigit():
            continue
        # Line 1: timecode
        tc_match = _SRT_TIMECODE_RE.fullmatch(lines[1].strip())
        if not tc_match:
            continue
        timecode = f"{tc_match.group(1)} --> {tc_match.group(2)}"
        text = "\n".join(lines[2:])
        blocks.append(
            _Cue(
                index=len(blocks),
                header=index_line,
                timecode=timecode,
                cue_settings="",
                text=text,
                fmt="srt",
            )
        )
    return blocks


def _serialize_srt(blocks: list[_Block]) -> str:
    """Re-serialize SRT from blocks.  Blank-line separated, trailing newline."""
    parts: list[str] = []
    cue_number = 1
    for block in blocks:
        if isinstance(block, _Cue):
            # block.header (original SRT index) is intentionally unused here:
            # renumbering 1..N is the standard SRT practice and keeps output
            # consistent after any filtering.  header is retained on _Cue so
            # the VTT path (_serialize_vtt) can emit cue identifiers verbatim.
            parts.append(f"{cue_number}\n{block.timecode}\n{block.text}")
            cue_number += 1
        # _Passthrough blocks are not expected in SRT (parser drops them)
        # but handle defensively
        elif isinstance(block, _Passthrough) and block.content.strip():
            parts.append(block.content)
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# WebVTT parser
# ---------------------------------------------------------------------------

# VTT timecode: HH:MM:SS.mmm --> HH:MM:SS.mmm [settings]
# Minutes/hours can be omitted per spec (MM:SS.mmm is valid).
_VTT_TIMECODE_RE = re.compile(
    r"(\d{2,}:\d{2}[:\d]*\.\d{1,3})"   # start (dots)
    r"\s*-->\s*"
    r"(\d{2,}:\d{2}[:\d]*\.\d{1,3})"   # end
    r"(.*)"                              # optional cue settings
)


def _parse_vtt(content: str) -> list[_Block]:
    """Parse WebVTT content into a mixed list of _Cue and _Passthrough blocks.

    Structure:
      - Header block: WEBVTT + optional description → _Passthrough
      - NOTE / STYLE blocks → _Passthrough
      - Cue blocks: optional cue identifier, timecode line, cue text → _Cue
      - Blank separators are consumed by the splitter (not stored separately)
    """
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    # Ensure we don't strip the WEBVTT header itself
    raw_blocks = re.split(r"\n{2,}", content)

    blocks: list[_Block] = []

    for raw in raw_blocks:
        raw = raw.strip()
        if not raw:
            continue

        # WEBVTT header block (first block always starts with WEBVTT)
        if raw.startswith("WEBVTT"):
            blocks.append(_Passthrough(index=len(blocks), content=raw))
            continue

        # NOTE / STYLE blocks
        if raw.startswith("NOTE") or raw.startswith("STYLE"):
            blocks.append(_Passthrough(index=len(blocks), content=raw))
            continue

        lines = raw.splitlines()

        # Try to find the timecode line (may be preceded by an optional cue id)
        tc_line_idx: int | None = None
        for i, line in enumerate(lines):
            if _VTT_TIMECODE_RE.match(line.strip()):
                tc_line_idx = i
                break

        if tc_line_idx is None:
            # No recognizable timecode → passthrough
            blocks.append(_Passthrough(index=len(blocks), content=raw))
            continue

        tc_match = _VTT_TIMECODE_RE.match(lines[tc_line_idx].strip())
        assert tc_match is not None  # guaranteed by the search above

        # Build the header: lines before the timecode (cue identifier, if any)
        header_lines = lines[:tc_line_idx]
        header = "\n".join(header_lines) if header_lines else ""

        timecode = f"{tc_match.group(1)} --> {tc_match.group(2)}"
        cue_settings = tc_match.group(3).strip()  # e.g. "align:start position:10%"

        text_lines = lines[tc_line_idx + 1:]
        text = "\n".join(text_lines)

        if not text.strip():
            # Empty cue — passthrough so structure is preserved
            # Reconstruct the raw block
            blocks.append(_Passthrough(index=len(blocks), content=raw))
            continue

        blocks.append(
            _Cue(
                index=len(blocks),
                header=header,
                timecode=timecode,
                cue_settings=cue_settings,
                text=text,
                fmt="vtt",
            )
        )

    return blocks


def _serialize_vtt(blocks: list[_Block]) -> str:
    """Re-serialize WebVTT from blocks.  Blank-line separated, trailing newline."""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, _Passthrough):
            parts.append(block.content)
        elif isinstance(block, _Cue):
            # Re-assemble: optional header + timecode (+ cue settings) + text
            tc_line = block.timecode
            if block.cue_settings:
                tc_line = f"{tc_line} {block.cue_settings}"
            if block.header:
                parts.append(f"{block.header}\n{tc_line}\n{block.text}")
            else:
                parts.append(f"{tc_line}\n{block.text}")
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Generic parse / serialize dispatchers
# ---------------------------------------------------------------------------

def parse(content: str, fmt: str) -> list[_Block]:
    """Dispatch to the correct parser."""
    if fmt == "srt":
        return _parse_srt(content)
    if fmt == "vtt":
        return _parse_vtt(content)
    raise SubtitleFormatError(f"Unknown format: {fmt!r}")


def serialize(blocks: list[_Block], fmt: str) -> str:
    """Dispatch to the correct serializer."""
    if fmt == "srt":
        return _serialize_srt(blocks)
    if fmt == "vtt":
        return _serialize_vtt(blocks)
    raise SubtitleFormatError(f"Unknown format: {fmt!r}")


# ---------------------------------------------------------------------------
# LLM translation helpers
# ---------------------------------------------------------------------------

def _encode_chunk(cues: list[_Cue]) -> tuple[str, list[str]]:
    """Build the numbered-list prompt payload for a chunk of cues.

    Intra-cue newlines are replaced by _NEWLINE_SENTINEL so each cue fits on
    one line in the numbered list.  Returns (encoded_body, original_texts)
    where original_texts preserves the sentinel-free originals for fallback.
    """
    lines: list[str] = []
    originals: list[str] = []
    for i, cue in enumerate(cues, start=1):
        encoded = cue.text.replace("\n", _NEWLINE_SENTINEL)
        lines.append(f"{i}. {encoded}")
        originals.append(cue.text)
    return "\n".join(lines), originals


def _build_prompt(encoded_body: str, n: int, target_lang: str, source_lang: str | None) -> str:
    """Build the full LLM prompt for a chunk."""
    source_clause = f" from {source_lang}" if source_lang else ""
    return (
        f"Translate each numbered subtitle line below{source_clause} to {target_lang}.\n"
        f"Return EXACTLY {n} numbered lines in the same order.\n"
        "Rules:\n"
        "- Do NOT translate proper nouns (names of people, places, brands).\n"
        "- Preserve all punctuation and inline tags (e.g. <i>, <b>, {\\an8}).\n"
        f"- Keep the sentinel '{_NEWLINE_SENTINEL}' exactly as-is wherever it appears.\n"
        "- Output ONLY the numbered list, nothing else.\n\n"
        f"{encoded_body}"
    )


# Leading-number pattern: "  3. text" or "3) text"
_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)[.)]\s*(.*)", re.MULTILINE)


def _parse_llm_response(response: str, expected_count: int) -> list[str] | None:
    """Extract the translated lines from the LLM response.

    Returns a list of `expected_count` strings, or None if the count mismatches.
    Each string has the _NEWLINE_SENTINEL preserved — the caller restores newlines.
    """
    matches = _NUMBERED_LINE_RE.findall(response)
    if not matches:
        return None

    # Build a dict so we tolerate out-of-order lines (rare but defensive).
    # First occurrence wins; duplicates are detected via the strict set check below.
    numbered: dict[int, str] = {}
    for num_str, text in matches:
        n = int(num_str)
        if n not in numbered:
            numbered[n] = text

    # Strict alignment: the parsed numbers must be EXACTLY the set {1..expected_count}.
    # Missing numbers, duplicate numbers, or extra numbers beyond expected_count all
    # trigger a mismatch → retry-then-fallback-to-original (no cue is ever dropped).
    if set(numbered.keys()) != set(range(1, expected_count + 1)):
        return None

    # Re-order 1..n (KeyError cannot happen after the strict check above)
    return [numbered[i] for i in range(1, expected_count + 1)]


def _restore_newlines(text: str) -> str:
    """Replace the newline sentinel back to real newlines.

    The LLM is asked to keep the sentinel verbatim, but smaller models often
    reproduce it with altered surrounding whitespace (or none at all), and may
    even append a stray one at the end of a line. Match the ``⏎`` glyph with any
    surrounding horizontal whitespace so those variants don't leak into the
    output, then trim stray leading/trailing whitespace/newlines the model may
    have introduced at the cue boundary (internal newlines are preserved).
    """
    text = re.sub(r"[ \t]*⏎[ \t]*", "\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Chunk translation (single chunk, with one retry)
# ---------------------------------------------------------------------------

async def _translate_chunk(
    cues: list[_Cue],
    target_lang: str,
    source_lang: str | None,
    semaphore: asyncio.Semaphore,
) -> list[str]:
    """Translate a chunk of cues.  Returns translated texts in cue order.

    Alignment safety:
      - If the LLM returns the wrong count, retry once.
      - If it still mismatches, fall back to original cue texts (no cue dropped).

    httpx / network exceptions propagate to the caller (endpoint maps to 503).
    Only alignment failures are caught and handled here.
    """
    encoded_body, originals = _encode_chunk(cues)
    n = len(cues)
    prompt = _build_prompt(encoded_body, n, target_lang, source_lang)

    async def _call() -> list[str] | None:
        raw = await ollama_service.generate(prompt)
        return _parse_llm_response(raw, n)

    async with semaphore:
        try:
            result = await asyncio.wait_for(
                _call(),
                timeout=settings.SUBTITLE_PER_CHUNK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Subtitle chunk translation timed out (%ds), keeping originals for %d cues",
                settings.SUBTITLE_PER_CHUNK_TIMEOUT,
                n,
            )
            return originals

        if result is None:
            logger.warning("Alignment mismatch on first attempt for %d-cue chunk, retrying", n)
            # Retry once inside the same semaphore slot
            try:
                result = await asyncio.wait_for(
                    _call(),
                    timeout=settings.SUBTITLE_PER_CHUNK_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Retry timed out, keeping originals for %d cues", n)
                return originals

        if result is None:
            logger.warning(
                "Alignment mismatch after retry for %d-cue chunk, keeping originals", n
            )
            return originals

        # Restore intra-cue newlines
        return [_restore_newlines(t) for t in result]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def translate_subtitles(
    content: str,
    *,
    target_lang: str = "fr",
    fmt: str | None = None,
    source_lang: str | None = None,
) -> SubtitleTranslation:
    """Parse, translate, and re-serialize a subtitle document.

    Parameters
    ----------
    content:
        Raw subtitle text (SRT or WebVTT).
    target_lang:
        BCP-47 language tag or human-readable name (e.g. "fr", "French").
    fmt:
        Force format; auto-detected from content when None.
    source_lang:
        Optional source language hint passed to the LLM prompt.

    Raises
    ------
    SubtitleFormatError   — empty content, zero parseable cues, unknown format.
    SubtitleTooLargeError — content exceeds SUBTITLE_MAX_BYTES or cue count
                            exceeds SUBTITLE_MAX_CUES.

    All httpx / Ollama exceptions propagate to the caller (WP4 maps to 503).
    """
    # --- Size guard (bytes) ---
    byte_size = len(content.encode("utf-8"))
    if byte_size > settings.SUBTITLE_MAX_BYTES:
        raise SubtitleTooLargeError(
            f"Content is {byte_size} bytes, exceeds limit of {settings.SUBTITLE_MAX_BYTES}"
        )

    # --- Empty content guard ---
    if not content.strip():
        raise SubtitleFormatError("Empty subtitle content")

    # --- Format detection ---
    detected_fmt = fmt if fmt is not None else detect_format(content)
    if detected_fmt not in ("srt", "vtt"):
        raise SubtitleFormatError(f"Unsupported subtitle format: {detected_fmt!r}")

    # --- Parse ---
    blocks = parse(content, detected_fmt)

    # Collect only translatable cues
    cues: list[_Cue] = [b for b in blocks if isinstance(b, _Cue)]

    if not cues:
        raise SubtitleFormatError("No translatable cues found in subtitle content")

    # --- Cue count guard ---
    if len(cues) > settings.SUBTITLE_MAX_CUES:
        raise SubtitleTooLargeError(
            f"Cue count {len(cues)} exceeds limit of {settings.SUBTITLE_MAX_CUES}"
        )

    logger.info(
        "Translating %d cues (%s → %s) fmt=%s model=%s",
        len(cues),
        source_lang or "auto",
        target_lang,
        detected_fmt,
        settings.OLLAMA_MODEL,
    )

    # --- Chunk cues ---
    chunk_size = max(1, settings.SUBTITLE_CHUNK_CUES)
    chunks: list[list[_Cue]] = [
        cues[i: i + chunk_size] for i in range(0, len(cues), chunk_size)
    ]

    # --- Concurrently translate chunks ---
    semaphore = asyncio.Semaphore(settings.SUBTITLE_CONCURRENCY)

    # Whole-translation deadline: if SUBTITLE_TOTAL_TIMEOUT is exceeded,
    # asyncio.TimeoutError propagates to the caller (endpoint maps it to 503).
    translated_chunks: list[list[str]] = await asyncio.wait_for(
        asyncio.gather(
            *[
                _translate_chunk(chunk, target_lang, source_lang, semaphore)
                for chunk in chunks
            ]
        ),
        timeout=settings.SUBTITLE_TOTAL_TIMEOUT,
    )

    # --- Flatten translated texts, maintaining correspondence to cues ---
    translated_texts: list[str] = []
    for chunk_texts in translated_chunks:
        translated_texts.extend(chunk_texts)

    # Safety: should never happen, but guard against gather length mismatch
    if len(translated_texts) != len(cues):
        logger.error(
            "gather result length %d != cue count %d — keeping originals",
            len(translated_texts),
            len(cues),
        )
        translated_texts = [c.text for c in cues]

    # --- Patch cue texts in blocks ---
    cue_iter = iter(zip(cues, translated_texts))
    patched_blocks: list[_Block] = []
    for block in blocks:
        if isinstance(block, _Cue):
            original_cue, new_text = next(cue_iter)
            # Create a new _Cue with the translated text; all other fields unchanged
            patched_blocks.append(
                _Cue(
                    index=original_cue.index,
                    header=original_cue.header,
                    timecode=original_cue.timecode,
                    cue_settings=original_cue.cue_settings,
                    text=new_text,
                    fmt=original_cue.fmt,
                )
            )
        else:
            patched_blocks.append(block)

    # --- Serialize ---
    translated_content = serialize(patched_blocks, detected_fmt)

    return SubtitleTranslation(
        translated_content=translated_content,
        fmt=detected_fmt,
        cue_count=len(cues),
        model=settings.OLLAMA_MODEL,
    )


# ---------------------------------------------------------------------------
# Cache pruning (called by daily cron job, master-only)
# ---------------------------------------------------------------------------

async def cleanup_cache(sessionmaker) -> int:
    """Delete ai_subtitle_cache rows older than SUBTITLE_CACHE_RETENTION_DAYS.

    If retention is 0, this is a no-op (keep forever) and returns 0.
    Uses commit_with_retry to handle SQLite write contention.
    Returns the number of rows deleted.
    """
    retention_days = settings.SUBTITLE_CACHE_RETENTION_DAYS
    if retention_days <= 0:
        logger.debug("Subtitle cache retention=0 — skipping pruning")
        return 0

    from app.models.database import AiSubtitleCache
    from app.utils.db_retry import commit_with_retry
    from app.utils.time import now_ms

    cutoff_ms = now_ms() - retention_days * 24 * 60 * 60 * 1000

    async with sessionmaker() as db:
        result = await db.execute(
            delete(AiSubtitleCache).where(AiSubtitleCache.created_at < cutoff_ms)
        )
        await commit_with_retry(db)

    deleted = result.rowcount
    logger.info("Subtitle cache cleanup: removed %d entries older than %d days", deleted, retention_days)
    return deleted
