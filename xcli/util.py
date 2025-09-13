import json
import os
import tempfile
import uuid
import socket
import errno
from datetime import datetime, timezone, timedelta
import re
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
    tzinfo = default_tz_from_name(tz_name)
    dt = dtparser.parse(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tzinfo)
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.isoformat(), (tz_name or "HKT")


def resolve_time_spec(ts: str, tz_name: Optional[str]) -> Tuple[str, str, bool]:
    """
    If ts is in HH:MM, resolve to the next occurrence of that time in the given tz.
    Returns (local_iso_with_tz, tz_used, was_shorthand).
    Otherwise, returns (ts, tz_used, False).
    """
    tz_used = tz_name or "HKT"
    if re.fullmatch(r"\d{1,2}:\d{2}", ts):
        tzinfo = default_tz_from_name(tz_used)
        now_local = datetime.now(tzinfo)
        h, m = map(int, ts.split(":"))
        target = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now_local:
            target = target + timedelta(days=1)
        return target.isoformat(), tz_used, True
    return ts, tz_used, False


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
