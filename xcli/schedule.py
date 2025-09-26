from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, timedelta

from .util import (
    load_schedule,
    save_schedule,
    parse_time_to_utc,
    gen_id,
    now_utc,
    compute_idempotency_key,
    resolve_since,
)


def add_job(text: str, at: str, tz_name: Optional[str]) -> Dict[str, Any]:
    schedule = load_schedule()
    time_utc_iso, tz_used = parse_time_to_utc(at, tz_name)
    dt_utc = datetime.fromisoformat(time_utc_iso)
    if dt_utc <= now_utc() + timedelta(minutes=5):
        raise ValueError("Scheduled time must be at least 5 minutes in the future")
    job_id = gen_id()
    idem = compute_idempotency_key(text, time_utc_iso)
    job = {
        "id": job_id,
        "text": text,
        "time_utc": time_utc_iso,
        "tz": tz_used,
        "status": "pending",
        "attempt_count": 0,
        "last_error": None,
        "posted_tweet_id": None,
        "idempotency_key": idem,
        "created_at": now_utc().isoformat(),
        "updated_at": now_utc().isoformat(),
    }
    schedule["jobs"].append(job)
    save_schedule(schedule)
    return job


def list_jobs(since: Optional[str] = None) -> List[Dict[str, Any]]:
    schedule = load_schedule()
    jobs = schedule.get("jobs", [])
    if since:
        rs = resolve_since(since, "UTC")
        cutoff = datetime.fromisoformat(rs) if rs else None
        jobs = [j for j in jobs if datetime.fromisoformat(j["time_utc"]) >= cutoff]
    return sorted(jobs, key=lambda j: j["time_utc"])  # type: ignore


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    schedule = load_schedule()
    for j in schedule.get("jobs", []):
        if j.get("id") == job_id:
            return j
    return None


def update_job(job_id: str, *, text: Optional[str], at: Optional[str], tz_name: Optional[str]) -> Dict[str, Any]:
    schedule = load_schedule()
    for j in schedule.get("jobs", []):
        if j.get("id") == job_id:
            if text is not None:
                j["text"] = text
            if at is not None:
                # Anchor relative specs (e.g., '1d CA evening') to the user's tz when --tz is not provided,
                # instead of the job's existing tz, to match monitor semantics.
                time_utc_iso, tz_used = parse_time_to_utc(at, tz_name or "HKT")
                j["time_utc"] = time_utc_iso
                j["tz"] = tz_used
            # Recompute idempotency key if content or time changed
            j["idempotency_key"] = compute_idempotency_key(j["text"], j["time_utc"])  # type: ignore
            j["updated_at"] = now_utc().isoformat()
            save_schedule(schedule)
            return j
    raise KeyError(f"Job {job_id} not found")


def remove_job(job_id: str) -> bool:
    schedule = load_schedule()
    before = len(schedule.get("jobs", []))
    schedule["jobs"] = [j for j in schedule.get("jobs", []) if j.get("id") != job_id]
    after = len(schedule["jobs"])
    save_schedule(schedule)
    return after < before
