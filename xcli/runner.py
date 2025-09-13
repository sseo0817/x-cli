from __future__ import annotations

from typing import Any, Dict, List
from datetime import datetime

from .util import (
    load_schedule,
    save_schedule,
    now_utc,
    acquire_lock,
    release_lock,
    read_lock_info,
    pid_alive,
    append_journal,
    journal_lookup_idempotency,
    update_lock_heartbeat,
)
from .api import post_tweet, ApiError


def runner_status() -> Dict[str, Any]:
    info = read_lock_info()
    if not info:
        return {"running": False}
    pid = info.get("pid")
    alive = pid_alive(pid) if isinstance(pid, int) else False
    return {"running": alive, **info}


def run_once(max_attempts_per_post: int = 2) -> Dict[str, Any]:
    pid, existing = acquire_lock()
    if pid is None:
        # Someone else is running
        return {"ok": False, "reason": "runner_active", "info": existing}
    try:
        run_started = now_utc().isoformat()
        schedule = load_schedule()
        # Initial heartbeat
        update_lock_heartbeat(expected_pid=pid)
        now = now_utc()
        due = [j for j in schedule.get("jobs", []) if j.get("status") == "pending" and datetime.fromisoformat(j["time_utc"]) <= now]  # type: ignore

        posted = []
        failed = []

        for j in due:
            # Idempotency check via journal
            idem = j.get("idempotency_key")
            if idem:
                rec = journal_lookup_idempotency(idem)
                if rec and rec.get("tweet_id"):
                    j["status"] = "posted"
                    j["posted_tweet_id"] = rec.get("tweet_id")
                    j["updated_at"] = now_utc().isoformat()
                    posted.append(j["id"])  # type: ignore
                    continue
            # Attempt post
            j["status"] = "in_progress"
            j["attempt_count"] = int(j.get("attempt_count", 0)) + 1
            try:
                tweet_id, raw = post_tweet(j["text"], max_attempts=max_attempts_per_post)
                # Append journal first
                append_journal({
                    "id": j["id"],
                    "idempotency_key": j.get("idempotency_key"),
                    "tweet_id": tweet_id,
                    "posted_at": now_utc().isoformat(),
                    "source": "scheduled",
                    "text": j.get("text"),
                })
                # Then mark job
                j["status"] = "posted"
                j["posted_tweet_id"] = tweet_id
                j["last_error"] = None
                j["updated_at"] = now_utc().isoformat()
                posted.append(j["id"])  # type: ignore
            except ApiError as e:
                j["status"] = "failed"
                j["last_error"] = str(e)
                j["updated_at"] = now_utc().isoformat()
                failed.append(j["id"])  # type: ignore
            finally:
                # Bump heartbeat after each job attempt
                update_lock_heartbeat(expected_pid=pid)
        save_schedule(schedule)
        ok = True
        res = {"ok": ok, "posted": posted, "failed": failed, "checked": len(due)}
        # Append a run journal entry for monitoring (exclude pure skips later)
        append_journal({
            "type": "run",
            "started_at": run_started,
            "posted_at": now_utc().isoformat(),
            "ok": ok,
            "checked": len(due),
            "posted_count": len(posted),
            "failed_count": len(failed),
            "posted_ids": posted,
            "failed_ids": failed,
            "message": f"posted={len(posted)} failed={len(failed)} checked={len(due)}",
            "skipped": (len(due) == 0 and len(posted) == 0 and len(failed) == 0),
        })
        # Final heartbeat before release
        update_lock_heartbeat(expected_pid=pid)
        return res
    finally:
        release_lock()
