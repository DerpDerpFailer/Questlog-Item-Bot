import json
import logging
import threading
import time

import requests

from questlog.api import API_TIMEOUT, BASE_URL

log = logging.getLogger(__name__)

STAT_FORMAT_TTL = 86400  # 24h in seconds
STAT_FORMAT_RETRY = 300  # seconds before retrying a failed stat-format refresh

# Stat formats cache
_stat_formats: dict = {}
_stat_formats_loaded_at: float = 0.0
_stat_formats_attempted_at: float = 0.0
_stat_formats_refreshing: bool = False
_stat_formats_lock = threading.Lock()


def load_stat_formats() -> None:
    global _stat_formats, _stat_formats_loaded_at
    try:
        r = requests.get(
            f"{BASE_URL}/statFormat.getStatFormat",
            params={"input": json.dumps({"language": "en"}, separators=(",", ":"))},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=API_TIMEOUT
        )
        r.raise_for_status()
        _stat_formats = r.json()["result"]["data"]
        _stat_formats_loaded_at = time.time()
        log.info(f"Loaded {len(_stat_formats)} stat formats")
    except Exception as e:
        log.warning(f"Could not load stat formats: {e}")


def _refresh_stat_formats_in_background() -> None:
    global _stat_formats_refreshing
    try:
        load_stat_formats()
    finally:
        with _stat_formats_lock:
            _stat_formats_refreshing = False


def get_stat_formats() -> dict:
    """Never blocks: called from async handlers, where a slow questlog.gg would otherwise
    freeze the whole bot for up to API_TIMEOUT seconds. When the formats are older than 24h
    the stale ones keep being served while one background thread refreshes them; a failed
    refresh is retried after STAT_FORMAT_RETRY seconds instead of on every call."""
    global _stat_formats_refreshing, _stat_formats_attempted_at
    now = time.time()
    stale = now - _stat_formats_loaded_at > STAT_FORMAT_TTL
    if stale and now - _stat_formats_attempted_at > STAT_FORMAT_RETRY:
        with _stat_formats_lock:
            if not _stat_formats_refreshing:
                _stat_formats_refreshing = True
                _stat_formats_attempted_at = now
                threading.Thread(target=_refresh_stat_formats_in_background, daemon=True).start()
    return _stat_formats


def format_stat(key: str, value: float) -> str:
    fmt = get_stat_formats().get(key)
    if not fmt:
        return f"{key}: {value}"
    name = fmt.get("name", key)
    multiplier = fmt.get("multiplier", 1)
    value_format = fmt.get("valueFormat", "{0}")
    computed = round(value * multiplier, 2)
    computed_str = str(int(computed)) if computed == int(computed) else str(computed)
    return f"{name}: {value_format.replace('{0}', computed_str)}"
