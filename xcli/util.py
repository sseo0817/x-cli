import json
import os
import tempfile
import uuid
import socket
import errno
from datetime import datetime, timezone, timedelta
import re
import random
from typing import Any, Dict, List, Optional, Tuple

from dateutil import parser as dtparser
from dateutil import tz as dttz


CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".x-cli")
# Attempt to ensure home config dir, fall back to project-local .x-cli if not permitted
try:
    os.makedirs(CONFIG_DIR, exist_ok=True)
except PermissionError:
    CONFIG_DIR = os.path.join(os.getcwd(), ".x-cli")
    os.makedirs(CONFIG_DIR, exist_ok=True)
SCHEDULE_PATH = os.path.join(CONFIG_DIR, "schedule.json")
JOURNAL_PATH = os.path.join(CONFIG_DIR, "journal.jsonl")
LOCK_PATH = os.path.join(CONFIG_DIR, "runner.lock")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Cron log is written to HOME-based directory by cron
CRON_LOG_PATH = os.path.join(os.path.expanduser("~"), ".x-cli", "cron.log")

def cron_log_default_path() -> str:
    return CRON_LOG_PATH


def ensure_config_dir() -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except PermissionError:
        # Already handled at import time; no-op.
        pass


def read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default


def write_json_atomic(path: str, data: Any) -> None:
    ensure_config_dir()
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def default_tz_from_name(name: Optional[str]) -> dttz.tzoffset:
    if not name or name.upper() == "HKT":
        return dttz.gettz("Asia/Hong_Kong")
    tz = dttz.gettz(name)
    if tz is None:
        # Fallback to UTC if unknown
        tz = dttz.UTC
    return tz


def parse_time_to_utc(ts: str, tz_name: Optional[str]) -> Tuple[str, str]:
    """Parse user input into UTC ISO + tz name.

    Supports:
    - Absolute ISO / 'YYYY-MM-DD HH:MM'
    - Shorthand 'HH:MM' (next occurrence in tz)
    - Prime time keywords (e.g., 'NY evening')
    - Day-offset prefixes: '{N}d HH:MM' or '{N}d {prime time}'
    """
    # Normalize with keywords and day offsets first to a local ISO string
    local_iso, tz_resolved, _ = resolve_time_spec(ts, tz_name)
    tzinfo = default_tz_from_name(tz_resolved)
    dt = dtparser.parse(local_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tzinfo)
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.isoformat(), (tz_resolved or "HKT")


def resolve_time_spec(ts: str, tz_name: Optional[str]) -> Tuple[str, str, bool]:
    """
    If ts is in HH:MM, resolve to the next occurrence of that time in the given tz.
    Returns (local_iso_with_tz, tz_used, was_shorthand).
    Otherwise, returns (ts, tz_used, False).
    """
    tz_used = tz_name or "HKT"
    # Day-offset prefix like '2d ...'
    days_offset = 0
    m_off = re.match(r"^(\d+)d\s+(.+)$", ts.strip(), re.IGNORECASE) if isinstance(ts, str) else None
    remainder = ts
    if m_off:
        days_offset = int(m_off.group(1))
        remainder = m_off.group(2)
    # Prime time keywords
    # Prime time keywords: always pick a random time within the chosen window
    kw_local, kw_tz, is_kw = _resolve_prime_time_keyword(
        remainder,
        prefer_earliest=False,
        days_offset=days_offset,
        anchor_tz=tz_used,
    )
    if is_kw:
        return kw_local, kw_tz or tz_used, True
    if re.fullmatch(r"\d{1,2}:\d{2}", ts):
        tzinfo = default_tz_from_name(tz_used)
        now_local = datetime.now(tzinfo)
        h, m = map(int, ts.split(":"))
        target = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now_local:
            target = target + timedelta(days=1)
        # Apply days offset if provided
        if days_offset:
            target = target + timedelta(days=days_offset - 1)  # we already advanced to next occurrence
        # Enforce at least 5 minutes in future
        if target <= now_local + timedelta(minutes=5):
            raise ValueError("Scheduled time must be at least 5 minutes in the future")
        return target.isoformat(), tz_used, True
    return ts, tz_used, False


_PRIME_WINDOWS = {
    # region: (tz, {period: (start_hour, end_hour_exclusive)})
    "EU": ("Europe/Berlin", {"morning": (8, 11), "noon": (12, 14), "evening": (18, 22)}),
    "NY": ("America/New_York", {"morning": (8, 11), "noon": (12, 14), "evening": (18, 22)}),
    "CA": ("America/Los_Angeles", {"morning": (8, 11), "noon": (12, 14), "evening": (18, 22)}),
    "ASIA": ("Asia/Hong_Kong", {"morning": (9, 12), "noon": (12, 14), "evening": (19, 22)}),
}

_REGION_ALIASES = {
    "EU": {"EU", "EUROPE"},
    "NY": {"NY", "NYC", "NEWYORK", "NEW_YORK"},
    "CA": {"CA", "CALIFORNIA", "SF", "BAY", "LA", "LOSANGELES", "LOS_ANGELES"},
    "ASIA": {"ASIA", "HK", "HONGKONG", "HONG_KONG", "SG", "SINGAPORE"},
}


def _match_region(token: str) -> Optional[str]:
    t = re.sub(r"\s+|[_-]", "", token).upper()
    for key, aliases in _REGION_ALIASES.items():
        if t in aliases:
            return key
    return None


def _resolve_prime_time_keyword(
    spec: str,
    *,
    prefer_earliest: bool = False,
    days_offset: int = 0,
    anchor_tz: Optional[str] = None,
) -> Tuple[str, Optional[str], bool]:
    """If spec matches a prime time keyword like 'EU morning', return a concrete
    local ISO time within the next occurrence of that window, and its timezone name.

    Returns (local_iso_with_tz, tz_used, is_keyword)
    """
    if not isinstance(spec, str):
        return spec, None, False  # type: ignore
    s = spec.strip()
    m = re.match(r"^(?P<region>[A-Za-z_\s]+)\s+(?P<part>morning|noon|non|evening)$", s, re.IGNORECASE)
    if not m:
        return spec, None, False
    region_raw = m.group("region")
    part = m.group("part").lower()
    if part == "non":
        part = "noon"
    region = _match_region(region_raw)
    if region is None or region not in _PRIME_WINDOWS:
        return spec, None, False
    tz_name = _PRIME_WINDOWS[region][0]
    tzinfo = dttz.gettz(tz_name)
    if tzinfo is None:
        tzinfo = dttz.UTC
    now_local = datetime.now(tzinfo)
    start_h, end_h = _PRIME_WINDOWS[region][1].get(part, (None, None))
    if start_h is None:
        return spec, None, False
    # Anchor base day with days_offset relative to anchor_tz (user tz) if provided
    if anchor_tz:
        atz = default_tz_from_name(anchor_tz)
        now_anch = datetime.now(atz)
        user_day_start = now_anch.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_offset)
        user_day_end = user_day_start + timedelta(days=1)
        # Initial guess: map user's day start to region date
        region_anchor = user_day_start.astimezone(tzinfo)
        base_day = region_anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        base_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_offset)
    start = base_day.replace(hour=start_h, minute=0, second=0, microsecond=0)
    end = base_day.replace(hour=end_h, minute=0, second=0, microsecond=0)
    # If anchoring to user's tz, ensure the region window's UTC end falls within the user's anchor UTC day
    if anchor_tz:
        u_start_utc = user_day_start.astimezone(timezone.utc)
        u_end_utc = user_day_end.astimezone(timezone.utc)
        # Adjust base_day by at most one day to fit end within [u_start_utc, u_end_utc)
        for _ in range(2):
            end_utc = end.astimezone(timezone.utc)
            if end_utc < u_start_utc:
                # region window too early; move forward a day
                start += timedelta(days=1)
                end += timedelta(days=1)
                base_day += timedelta(days=1)
                continue
            if end_utc >= u_end_utc:
                # region window ends after the user's day; move back a day
                start -= timedelta(days=1)
                end -= timedelta(days=1)
                base_day -= timedelta(days=1)
                continue
            break
    # If now is already past the base window (and days_offset is 0), shift to next day
    if days_offset == 0:
        earliest = max(start, now_local + timedelta(minutes=5))
        if earliest >= end:
            start = start + timedelta(days=1)
            end = end + timedelta(days=1)
            earliest = start
    else:
        earliest = start
        # For exact future date, enforce 5-minute rule only if it's today
        if (start.date() == now_local.date()) and earliest <= now_local + timedelta(minutes=5):
            raise ValueError("Scheduled time must be at least 5 minutes in the future")
    # Ensure N-day semantics in elapsed time when anchoring to user's tz
    if anchor_tz and days_offset > 0:
        min_dt_reg = (datetime.now(timezone.utc) + timedelta(days=days_offset, minutes=5)).astimezone(tzinfo)
        if earliest < min_dt_reg:
            earliest = min_dt_reg
            if earliest >= end:
                # move to next day's window
                start = start + timedelta(days=1)
                end = end + timedelta(days=1)
                earliest = start
    # Choose time inside window
    if prefer_earliest:
        target = earliest
    else:
        total_seconds = int((end - earliest).total_seconds())
        if total_seconds <= 60:
            target = earliest
        else:
            offset = random.randint(0, total_seconds - 60)
            target = earliest + timedelta(seconds=offset)
    return target.isoformat(), tz_name, True


def iso_utc_to_local_str(iso_utc: str, tz_name: Optional[str]) -> str:
    tzinfo = default_tz_from_name(tz_name)
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tzinfo).isoformat()


def iso_utc_to_local_hms(iso_utc: str, tz_name: Optional[str]) -> str:
    """Convert an ISO UTC time to local 'YYYY-MM-DD HH:MM:SS'."""
    tzinfo = default_tz_from_name(tz_name)
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local_dt = dt.astimezone(tzinfo)
    return local_dt.strftime("%Y-%m-%d %H:%M:%S")


def gen_id() -> str:
    return uuid.uuid4().hex[:12]


def load_schedule() -> Dict[str, Any]:
    ensure_config_dir()
    data = read_json(SCHEDULE_PATH, {"jobs": []})
    if "jobs" not in data or not isinstance(data["jobs"], list):
        data = {"jobs": []}
    return data


def save_schedule(schedule: Dict[str, Any]) -> None:
    write_json_atomic(SCHEDULE_PATH, schedule)


def append_journal(entry: Dict[str, Any]) -> None:
    ensure_config_dir()
    # Append atomically by writing to a temp and then appending via os.replace is tricky; use append with fsync.
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def journal_lookup_idempotency(key: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(JOURNAL_PATH):
        return None
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("idempotency_key") == key:
                    return rec
    except OSError:
        return None
    return None


def read_journal(since_iso: Optional[str] = None) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not os.path.exists(JOURNAL_PATH):
        return items
    cutoff: Optional[datetime] = None
    if since_iso:
        try:
            cutoff = datetime.fromisoformat(parse_time_to_utc(since_iso, "UTC")[0])
        except Exception:
            cutoff = None
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cutoff is not None:
                    try:
                        ts = rec.get("posted_at")
                        if ts and datetime.fromisoformat(ts) < cutoff:
                            continue
                    except Exception:
                        pass
                items.append(rec)
    except OSError:
        pass
    # Sort by posted_at
    items.sort(key=lambda r: r.get("posted_at", ""))
    return items


def journal_find_by_id(entry_id: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(JOURNAL_PATH):
        return None
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("id") == entry_id:
                    return rec
    except OSError:
        return None
    return None


_REL_RE = re.compile(r"^(\d+)([smhd])$", re.IGNORECASE)


def resolve_since(spec: Optional[str], tz_name: Optional[str]) -> Optional[str]:
    if not spec:
        return None
    m = _REL_RE.match(spec)
    if m:
        qty = int(m.group(1))
        unit = m.group(2).lower()
        delta = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]
        now = now_utc()
        td = timedelta(**{delta: qty})
        return (now - td).isoformat()
    # Fallback to absolute time parsing
    try:
        iso_utc, _ = parse_time_to_utc(spec, tz_name or "UTC")
        return iso_utc
    except Exception:
        return None


def compute_idempotency_key(text: str, time_utc_iso: str) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(text.encode("utf-8"))
    h.update(b"|")
    h.update(time_utc_iso.encode("utf-8"))
    return h.hexdigest()


def acquire_lock() -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    ensure_config_dir()
    pid = os.getpid()
    started_at = now_utc().isoformat()
    hostname = socket.gethostname()
    data = {"pid": pid, "hostname": hostname, "started_at": started_at, "last_heartbeat": started_at}
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(LOCK_PATH, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            os.close(fd)
            raise
        return pid, data
    except OSError as e:
        if e.errno == errno.EEXIST:
            # Lock exists; read and return info
            try:
                with open(LOCK_PATH, "r", encoding="utf-8") as f:
                    info = json.load(f)
            except Exception:
                info = None
            return None, info
        raise


def release_lock() -> None:
    try:
        os.unlink(LOCK_PATH)
    except FileNotFoundError:
        pass


def update_lock_heartbeat(expected_pid: Optional[int] = None) -> None:
    """Update last_heartbeat in the lock file if it exists.

    If expected_pid is provided, only update when the lock's pid matches.
    """
    if not os.path.exists(LOCK_PATH):
        return
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            info = json.load(f)
        if expected_pid is not None and info.get("pid") != expected_pid:
            return
        info["last_heartbeat"] = now_utc().isoformat()
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            json.dump(info, f)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        return


def read_lock_info() -> Optional[Dict[str, Any]]:
    if not os.path.exists(LOCK_PATH):
        return None
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
