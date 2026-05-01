"""Cache layer for the events list.

Goal:
- Page loads ALWAYS read from cache first (no waiting on Google Calendar).
- A background thread refreshes the cache at most once per
  ``events_refresh_min_seconds`` seconds.
- Clients poll a freshness endpoint; when ``updated_at`` advances they reload.

Backend selection:
- If ``REDIS_URL`` is set → Redis (production: survives restarts, shared
  across processes).
- Otherwise → SQL (the project's existing Postgres / SQLite, table
  ``kv_cache``). Persistent across server restarts so the events page is
  instant even after a redeploy.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable

from app.settings import get_settings

log = logging.getLogger(__name__)

_KEY_DATA = "linh_news:events:data"
_KEY_META = "linh_news:events:meta"  # {"updated_at": float, "in_progress": bool}
_KEY_MOVIES_DATA = "linh_news:movies:data"
_KEY_MOVIES_META = "linh_news:movies:meta"
_MOVIES_REFRESH_MIN_SECONDS = 24 * 60 * 60  # at most once per day
_KEY_REFRESH = "linh_news:edition_refresh"  # {"started_at": float, "worker_pid": int}
_KEY_REFRESH_ERR = "linh_news:edition_refresh_error"
_KEY_REFRESH_DURATIONS = "linh_news:edition_refresh_durations"
_REFRESH_DURATION_SAMPLES = 20
# Recent failures stay surface-able to the user for this window.
_REFRESH_ERROR_FRESH = 5 * 60  # seconds

# Edition generation should never legitimately take longer than this. Anything
# older is treated as a dead/abandoned refresh (e.g. the process was killed).
_REFRESH_STALE_AFTER = 12 * 60  # seconds (12 minutes)


class _SqlBackend:
    """Persistent cache backed by the project's SQL database (kv_cache table)."""

    def get(self, key: str) -> str | None:
        from app.db import KvCache, session_factory

        with session_factory()() as s:
            row = s.get(KvCache, key)
            return row.value if row else None

    def set(self, key: str, value: str) -> None:
        from app.db import KvCache, session_factory

        with session_factory()() as s:
            row = s.get(KvCache, key)
            if row:
                row.value = value
            else:
                s.add(KvCache(key=key, value=value))
            s.commit()


class _RedisBackend:
    def __init__(self, url: str) -> None:
        import redis

        self._r = redis.Redis.from_url(url, decode_responses=True)

    def get(self, key: str) -> str | None:
        return self._r.get(key)

    def set(self, key: str, value: str) -> None:
        self._r.set(key, value)


_backend: _SqlBackend | _RedisBackend | None = None
_refresh_lock = threading.Lock()


def _get_backend() -> _SqlBackend | _RedisBackend:
    global _backend
    if _backend is None:
        url = get_settings().redis_url
        if url:
            try:
                _backend = _RedisBackend(url)
                log.info("Cache backend: Redis at %s", url)
            except Exception as e:  # noqa: BLE001
                log.warning("Redis unavailable (%s); falling back to SQL cache.", e)
                _backend = _SqlBackend()
        else:
            _backend = _SqlBackend()
            log.info("Cache backend: SQL (kv_cache table)")
    return _backend


def reset_backend_for_tests() -> None:
    """Test hook to drop the cached backend after a session swap."""
    global _backend
    _backend = None


# ─────────── Edition-refresh state (survives process restarts) ───────────


def _pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is currently alive.

    POSIX uses ``os.kill(pid, 0)`` (a no-op signal that just probes existence).
    On Windows, ``os.kill`` is mapped to ``TerminateProcess`` and would
    actually kill the process — so we use the Win32 API to read the exit
    code without modifying the process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_win(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but we can't signal it
    except OSError:
        return False
    return True


def _pid_alive_win(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def edition_refresh_in_progress() -> bool:
    """True if a refresh started recently AND its worker process is alive.

    A VM restart (e.g. Fly deploy) kills the worker — the stored PID won't
    exist on the new VM, so we treat the lock as cleared immediately rather
    than waiting out the 12-min stale timeout. uvicorn --reload doesn't kill
    the worker (different process group), so its PID remains alive."""
    raw = _get_backend().get(_KEY_REFRESH)
    if not raw:
        return False
    try:
        meta = json.loads(raw)
        started = float(meta.get("started_at", 0))
        pid = int(meta.get("worker_pid", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return False
    if (time.time() - started) >= _REFRESH_STALE_AFTER:
        return False
    if pid > 0 and not _pid_alive(pid):
        # Worker died (VM restart, OOM kill, etc.) — clear the lock.
        end_edition_refresh()
        return False
    return True


def begin_edition_refresh() -> bool:
    """Atomically claim the refresh slot. Returns True if claimed, False if
    another refresh is already in flight (and not stale). The caller is
    expected to call ``set_edition_refresh_pid`` once it spawns the worker."""
    if edition_refresh_in_progress():
        return False
    _get_backend().set(
        _KEY_REFRESH,
        json.dumps({"started_at": time.time(), "worker_pid": 0}),
    )
    return True


def set_edition_refresh_pid(pid: int) -> None:
    """Record the worker subprocess's PID so we can detect dead workers
    (VM restart, OOM, kill -9) without waiting out the 12-min stale timer."""
    raw = _get_backend().get(_KEY_REFRESH)
    try:
        meta = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        meta = {}
    meta["worker_pid"] = int(pid)
    if "started_at" not in meta:
        meta["started_at"] = time.time()
    _get_backend().set(_KEY_REFRESH, json.dumps(meta))


def end_edition_refresh() -> None:
    _get_backend().set(_KEY_REFRESH, json.dumps({"started_at": 0, "worker_pid": 0}))


def set_edition_refresh_error(message: str) -> None:
    """Persist the latest refresh error so the UI can surface it as a toast."""
    _get_backend().set(
        _KEY_REFRESH_ERR,
        json.dumps({"message": str(message)[:500], "at": time.time()}),
    )


def get_recent_edition_refresh_error() -> str | None:
    """Return the most recent refresh error message if it occurred within
    the last few minutes; otherwise ``None``."""
    raw = _get_backend().get(_KEY_REFRESH_ERR)
    if not raw:
        return None
    try:
        meta = json.loads(raw)
        at = float(meta.get("at", 0))
        msg = meta.get("message")
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not msg or (time.time() - at) > _REFRESH_ERROR_FRESH:
        return None
    return str(msg)


def clear_edition_refresh_error() -> None:
    _get_backend().set(_KEY_REFRESH_ERR, "")


def record_refresh_duration(seconds: float) -> None:
    """Append a successful-refresh duration to the rolling window."""
    raw = _get_backend().get(_KEY_REFRESH_DURATIONS)
    try:
        samples = list(json.loads(raw)) if raw else []
    except (ValueError, TypeError, json.JSONDecodeError):
        samples = []
    samples.append(round(float(seconds), 2))
    samples = samples[-_REFRESH_DURATION_SAMPLES:]
    _get_backend().set(_KEY_REFRESH_DURATIONS, json.dumps(samples))


def expected_refresh_seconds() -> float | None:
    """Return the mean of the last N successful refresh durations, or None if
    we don't have any samples yet."""
    raw = _get_backend().get(_KEY_REFRESH_DURATIONS)
    if not raw:
        return None
    try:
        samples = list(json.loads(raw))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not samples:
        return None
    return sum(float(s) for s in samples) / len(samples)


def get_events() -> tuple[list[dict] | None, float]:
    """Return (events_or_None, updated_at_epoch). None means never cached."""
    b = _get_backend()
    data_raw = b.get(_KEY_DATA)
    meta_raw = b.get(_KEY_META)
    events = json.loads(data_raw) if data_raw else None
    meta = json.loads(meta_raw) if meta_raw else {}
    return events, float(meta.get("updated_at", 0))


def store_events(events: list[dict]) -> float:
    b = _get_backend()
    now = time.time()
    b.set(_KEY_DATA, json.dumps(events, default=str))
    b.set(_KEY_META, json.dumps({"updated_at": now, "in_progress": False}))
    return now


def get_movies() -> tuple[list[dict] | None, float]:
    b = _get_backend()
    data_raw = b.get(_KEY_MOVIES_DATA)
    meta_raw = b.get(_KEY_MOVIES_META)
    movies = json.loads(data_raw) if data_raw else None
    meta = json.loads(meta_raw) if meta_raw else {}
    return movies, float(meta.get("updated_at", 0))


def store_movies(movies: list[dict]) -> float:
    b = _get_backend()
    now = time.time()
    b.set(_KEY_MOVIES_DATA, json.dumps(movies, default=str))
    b.set(_KEY_MOVIES_META, json.dumps({"updated_at": now}))
    return now


def movies_cache_age() -> float | None:
    """Return seconds since last refresh, or None if never cached."""
    _, updated = get_movies()
    return None if updated == 0 else time.time() - updated


def movies_should_refresh() -> bool:
    age = movies_cache_age()
    return age is None or age >= _MOVIES_REFRESH_MIN_SECONDS


def maybe_refresh_in_background(
    fetch: Callable[[], list[dict]],
    *,
    min_interval: int | None = None,
) -> None:
    """If the last refresh is older than ``min_interval`` seconds, kick a
    background thread to refetch. Returns immediately."""
    interval = min_interval or get_settings().events_refresh_min_seconds
    _, updated_at = get_events()
    if time.time() - updated_at < interval:
        return
    if not _refresh_lock.acquire(blocking=False):
        return  # another refresh is already in flight

    def _run() -> None:
        try:
            log.info("Refreshing events cache in background...")
            events = fetch()
            store_events(events)
            log.info("Cache refresh complete: %d events.", len(events))
        except Exception as e:  # noqa: BLE001
            log.exception("Background events refresh failed: %s", e)
        finally:
            _refresh_lock.release()

    threading.Thread(target=_run, daemon=True, name="events-refresh").start()
