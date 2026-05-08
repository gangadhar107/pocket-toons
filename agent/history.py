"""
agent/history.py
----------------
Append-only JSON log of agent turns. Powers the Streamlit sidebar's
"Recent" list — re-ask on click; no thread resume, no multi-turn state.

Schema of each entry:
    {
      "ts":       ISO 8601 UTC timestamp,
      "question": str,
      "sql":      str | null,        // null for clarifications
      "summary":  str,               // agent summary or clarification text
      "ok":       bool               // self_check.ok, or false for clarify
    }

Storage: .cache/history.json, written atomically-ish (write + replace).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HISTORY_PATH = Path(".cache") / "history.json"
MAX_RECENT = 10


@dataclass(frozen=True)
class HistoryEntry:
    ts: str
    question: str
    sql: Optional[str]
    summary: str
    ok: bool


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _load_all(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            return []
        return data
    except (json.JSONDecodeError, OSError):
        # Corrupt file — don't crash the agent; start fresh.
        return []


def _atomic_write(path: Path, data: list[dict]) -> None:
    """Write atomically when we can (mkstemp + os.replace).

    If os.replace is blocked by the filesystem/sandbox (PermissionError /
    OSError), fall back to a plain write. We'd rather have a non-atomic
    history log than no history log; a crash between write and rename is a
    negligible failure mode for an append-only demo log.
    """
    _ensure_parent(path)
    # Try the atomic path first.
    fd, tmp = tempfile.mkstemp(prefix="history.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        try:
            os.replace(tmp, path)
            return
        except (OSError, PermissionError):
            # Rename not permitted — clean up tempfile and fall through.
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise

    # Non-atomic fallback.
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def append(
    question: str,
    sql: Optional[str],
    summary: str,
    ok: bool,
    path: Path = HISTORY_PATH,
) -> HistoryEntry:
    """Append a turn. Never raises; swallows I/O errors silently."""
    entry = HistoryEntry(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        question=question,
        sql=sql,
        summary=summary,
        ok=ok,
    )
    try:
        data = _load_all(path)
        data.append(entry.__dict__)
        _atomic_write(path, data)
    except Exception:
        pass
    return entry


def recent(n: int = MAX_RECENT, path: Path = HISTORY_PATH) -> list[HistoryEntry]:
    """Most-recent-first list of entries, capped at `n` (default 10)."""
    data = _load_all(path)
    out: list[HistoryEntry] = []
    for row in reversed(data):
        try:
            out.append(HistoryEntry(
                ts=row["ts"],
                question=row["question"],
                sql=row.get("sql"),
                summary=row.get("summary", ""),
                ok=bool(row.get("ok", False)),
            ))
        except (KeyError, TypeError):
            continue
        if len(out) >= n:
            break
    return out


def clear(path: Path = HISTORY_PATH) -> None:
    """Delete the history file. Useful for demos / tests."""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
