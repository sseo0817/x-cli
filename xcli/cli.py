from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from datetime import datetime, timedelta
import re
import os
import time
import stat

from .schedule import add_job, list_jobs, get_job, update_job, remove_job
from .runner import run_once, runner_status
from .api import post_tweet, ApiError, get_tweet, auth_status
from .utils.openai_client import LLMClient
from .cronctl import cron_on, cron_off, cron_status
from .util import append_journal, now_utc, gen_id, read_journal, resolve_time_spec, parse_time_to_utc, iso_utc_to_local_str, resolve_since, journal_find_by_id, cron_log_default_path, iso_utc_to_local_hms, load_schedule, default_tz_from_name


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def confirm(question: str, default: bool = False) -> bool:
    prompt = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"{question} {prompt} ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def cmd_schedule(args: argparse.Namespace) -> int:
    if args.action == "assign":
        if not args.text or not args.at:
            print("--text and --at are required", file=sys.stderr)
            return 2
        # preview and confirm
        if args.json and not getattr(args, "yes", False):
            print("--json mode requires --yes for assign confirmation", file=sys.stderr)
            return 2
        # Resolve shorthand HH:MM to the earliest next occurrence in tz
        local_spec, tz_used, was_short = resolve_time_spec(args.at, args.tz)
        # Compute UTC and relative time for display
        utc_iso, _ = parse_time_to_utc(local_spec, tz_used)
        try:
            target_local = datetime.fromisoformat(local_spec)
        except Exception:
            target_local = None
        rel = None
        if target_local is not None and target_local.tzinfo is not None:
            now_local = datetime.now(target_local.tzinfo)
            delta = target_local - now_local
            # clamp negative small offsets to zero
            if delta.total_seconds() < 0:
                delta = delta * 0
            rel = humanize_delta(int(delta.total_seconds()))
        # Compute simple length stats
        wc = len(args.text.split())
        cc = len(args.text)
        cc_str = str(cc)
        if _use_color():
            cc_str = f"\033[31m{cc}\033[0m" if cc > 280 else f"\033[32m{cc}\033[0m"
        # Preview with clear separators for multi-line text
        preview = (
            "\033[1m\033[36mAbout to schedule:\033[0m\n"
            + f"  at:   {local_spec} (tz={tz_used}) -> utc:{utc_iso}\n"
            + (f"  when: {rel}\n" if rel else "")
            + f"  length: words={wc} chars={cc_str}\n"
            + "  text:\n"
            + "\033[2m" + ("─" * 40) + "\033[0m\n"
            + f"{args.text}\n"
            + "\033[2m" + ("─" * 40) + "\033[0m"
        )
        if not getattr(args, "yes", False):
            print(preview)
            if not confirm("Proceed to add this schedule?", default=False):
                print("\033[33maborted\033[0m")
                return 0
        # Use the resolved local spec when adding
        job = add_job(args.text, local_spec, tz_used)
        if args.json:
            print_json(job)
        else:
            # Show local timezone time for clarity
            local_at = iso_utc_to_local_hms(job['time_utc'], job['tz']) if job.get('time_utc') else ''
            print(f"\033[32mscheduled: id={job['id']} at_local={local_at} tz={job['tz']}\033[0m")
        return 0
    elif args.action == "monitor":
        if args.id:
            job = get_job(args.id)
            if not job:
                print("\033[31mjob not found\033[0m", file=sys.stderr)
                return 1
            print_json(job) if args.json else print(format_job(job, tz=args.tz))
            return 0
        else:
            jobs = list_jobs(args.since)
            if args.json:
                print_json(jobs)
            else:
                print(format_jobs_table(jobs, tz=args.tz))
            return 0
    elif args.action == "update":
        if not args.id:
            print("--id is required", file=sys.stderr)
            return 2
        try:
            j = update_job(args.id, text=args.text, at=args.at, tz_name=args.tz)
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 1
        if args.json:
            print_json(j)
        else:
            print(f"\033[32mupdated: id={j['id']} at={j['time_utc']} tz={j['tz']}\033[0m")
        return 0
    elif args.action == "remove":
        if not args.id:
            print("--id is required", file=sys.stderr)
            return 2
        ok = remove_job(args.id)
        if not ok:
            print("\033[31mjob not found\033[0m", file=sys.stderr)
            return 1
        print("\033[32mremoved\033[0m")
        return 0
    else:
        print("unknown schedule action", file=sys.stderr)
        return 2


def format_job(j: dict, tz: str | None = None) -> str:
    when_local = iso_utc_to_local_hms(j.get('time_utc', ''), tz or j.get('tz')) if j.get('time_utc') else ''
    base = f"{j.get('id')} | {j.get('status')} | when:{when_local} tz:{tz or j.get('tz')}"
    if j.get("status") == "posted":
        tid = j.get("posted_tweet_id")
        url = f"https://x.com/i/web/status/{tid}" if tid else ""
        return base + (f" | {url}" if url else "")
    if j.get("status") == "failed":
        return base + f" | error: {j.get('last_error')}"
    return base


def format_jobs_table(rows: list[dict], tz: str | None = None) -> str:
    label_tz = tz or "HKT"
    headers = ["ID", "STATUS", f"WHEN({label_tz})", "TZ", "INFO"]
    data = []
    for j in rows:
        info = ""
        if j.get("status") == "posted" and j.get("posted_tweet_id"):
            info = f"https://x.com/i/web/status/{j.get('posted_tweet_id')}"
        elif j.get("status") == "failed" and j.get("last_error"):
            e = str(j.get("last_error"))
            info = e if len(e) <= 80 else e[:77] + "..."
        when_local = iso_utc_to_local_hms(j.get("time_utc", ""), tz or j.get("tz")) if j.get("time_utc") else ""
        data.append([
            j.get("id", ""),
            j.get("status", ""),
            when_local,
            tz or j.get("tz", ""),
            info,
        ])
    colw = [len(h) for h in headers]
    for row in data:
        for i, cell in enumerate(row):
            colw[i] = max(colw[i], len(str(cell)))
    def fmt_row(row: list[str]) -> str:
        return "  ".join(str(cell).ljust(colw[i]) for i, cell in enumerate(row))
    header_line = fmt_row(headers)
    sep_line = fmt_row(["-" * w for w in colw])
    if _use_color():
        header_line = f"\033[1;36m{header_line}\033[0m"  # bold cyan
        sep_line = f"\033[2m{sep_line}\033[0m"          # dim
    lines = [header_line, sep_line]
    for row in data:
        lines.append(fmt_row(row))
    return "\n".join(lines)


def _text_snippet(text: str, width: int = 40) -> str:
    first = text.splitlines()[0] if text else ""
    return (first[:width] + ("..." if first else "")) if len(first) > 0 else ""


# Non-overlapping prime time slots in UTC, in order
_PRIME_SLOTS = [
    ("NY evening", 22, 1),   # wraps to next day
    ("CA evening", 1, 5),
    ("Asia morning", 5, 8),
    ("EU morning", 8, 11),
    ("EU noon", 11, 12),
    ("NY morning", 12, 15),
    ("CA morning", 15, 19),
    ("CA noon", 19, 22),
]


def _prime_slot_bounds_utc(day0: datetime, start_h: int, end_h: int) -> tuple[datetime, datetime]:
    """Return (start,end) for the slot whose LABEL is this day0 (UTC midnight of label day).

    For wrap slots (e.g., 22→01), the label corresponds to the END date, so:
      start = (day0 - 1day) at 22:00, end = day0 at 01:00.
    For non-wrap, both start/end are on day0.
    """
    if start_h <= end_h:
        return day0.replace(hour=start_h), day0.replace(hour=end_h)
    prev = day0 - timedelta(days=1)
    return prev.replace(hour=start_h), day0.replace(hour=end_h)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _ansi_strip(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _pad_ansi(s: str, width: int, align: str = "left") -> str:
    raw = _ansi_strip(s)
    pad = max(0, width - len(raw))
    if align == "center":
        left = pad // 2
        right = pad - left
        return (" " * left) + s + (" " * right)
    elif align == "right":
        return (" " * pad) + s
    return s + (" " * pad)


def _print_prime_time_coverage(days: int = 10) -> None:
    # Prepare future days starting today (UTC midnights)
    now = now_utc()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_utc = [day0 + timedelta(days=i) for i in range(days)]
    dates = [d.strftime("%m-%d") for d in days_utc]
    date_labels = [f"{dates[i]} ({i}d)" for i in range(len(dates))]
    # Collect future pending jobs
    sched = load_schedule()
    jobs = [j for j in sched.get("jobs", []) if j.get("status") == "pending"]
    # Column widths (slightly wider to give breathing room)
    label_w = max(14, max(len(lbl) for lbl, *_ in _PRIME_SLOTS))
    date_w = max(10, max(len(s) for s in date_labels))
    colw = [label_w] + [date_w] * len(date_labels)

    # Helpers to draw box borders (no ANSI inside borders)
    def border(top: bool = False, mid: bool = False, bottom: bool = False) -> str:
        if top:
            left, sep, right, fill = "┌", "┬", "┐", "─"
        elif bottom:
            left, sep, right, fill = "└", "┴", "┘", "─"
        elif mid:
            left, sep, right, fill = "├", "┼", "┤", "─"
        else:
            left, sep, right, fill = "│", "│", "│", " "
        parts = [left]
        for i, w in enumerate(colw):
            parts.append(fill * w)
            parts.append(sep if i < len(colw) - 1 else right)
        return "".join(parts)

    def fmt_row(cells: list[str]) -> str:
        parts = ["│"]
        for i, c in enumerate(cells):
            # Center align all cells for a uniform look
            parts.append(_pad_ansi(c, colw[i], align="center"))
            parts.append("│")
        return "".join(parts)

    # Header
    print(border(top=True))
    hdr_cells = ["Prime / UTC"] + date_labels
    if _use_color():
        hdr_cells[0] = "\033[1;36m" + hdr_cells[0] + "\033[0m"
        hdr_cells[1:] = ["\033[2m" + d + "\033[0m" for d in hdr_cells[1:]]
    print(fmt_row(hdr_cells))
    print(border(mid=True))

    # Rows per slot
    for label, sh, eh in _PRIME_SLOTS:
        label_cell = ("\033[36m" + label + "\033[0m") if _use_color() else label
        cells = [label_cell]
        for i, d0 in enumerate(days_utc):
            start, end = _prime_slot_bounds_utc(d0, sh, eh)
            # Is there any pending job in [start, end)?
            has = False
            for j in jobs:
                t = j.get("time_utc")
                if not t:
                    continue
                try:
                    dt = datetime.fromisoformat(t)
                except Exception:
                    continue
                if start <= dt < end:
                    has = True
                    break
            # Use double block for better visibility
            symbol = "██"
            # Grey if slot already past relative to now (end no later than now+5m)
            past = end <= (now + timedelta(minutes=5))
            if _use_color():
                if past:
                    symbol = f"\033[90m{symbol}\033[0m"  # grey
                else:
                    symbol = f"\033[32m{symbol}\033[0m" if has else f"\033[31m{symbol}\033[0m"
            cells.append(symbol)
        print(fmt_row(cells))
    print(border(bottom=True))

    # Legend
    if _use_color():
        green = "\033[32m██\033[0m"
        red = "\033[31m██\033[0m"
        grey = "\033[90m██\033[0m"
    else:
        green = red = grey = "██"
    legend = f"Legend: {green}=scheduled  {red}=empty  {grey}=past"
    print(legend)


def format_journal_table(rows: list[dict], tz: str | None = None) -> str:
    label_tz = tz or "HKT"
    headers = [f"WHEN({label_tz})", "STATUS", "SOURCE", "ID", "URL", "TEXT"]
    data = []
    for r in rows:
        tid = r.get("tweet_id", "")
        url = f"https://x.com/i/web/status/{tid}" if tid else ""
        text = _text_snippet(r.get("text") or "")
        when = r.get("posted_at", "")
        if when:
            when_local = iso_utc_to_local_hms(when, tz)
        else:
            when_local = ""
        data.append([
            when_local,
            r.get("status", "posted"),
            r.get("source", ""),
            r.get("id", ""),
            url,
            text,
        ])
    colw = [len(h) for h in headers]
    for row in data:
        for i, cell in enumerate(row):
            colw[i] = max(colw[i], len(str(cell)))
    def fmt_row(row: list[str]) -> str:
        return "  ".join(str(cell).ljust(colw[i]) for i, cell in enumerate(row))
    header_line = fmt_row(headers)
    sep_line = fmt_row(["-" * w for w in colw])
    if _use_color():
        header_line = f"\033[1;36m{header_line}\033[0m"
        sep_line = f"\033[2m{sep_line}\033[0m"
    lines = [header_line, sep_line]
    for row in data:
        lines.append(fmt_row(row))
    return "\n".join(lines)


def cmd_monitor(args: argparse.Namespace) -> int:
    # Resolve since to ISO UTC for consistent filtering
    rsince = resolve_since(args.since, args.tz or "UTC")
    all_items = read_journal(rsince)

    # Extract run summaries
    all_runs = [r for r in all_items if r.get("type") == "run"]
    all_runs.sort(key=lambda r: r.get("posted_at", ""))
    # For display, exclude pure skips
    run_logs = [r for r in all_runs if not r.get("skipped")]

    # Tweets/journal (posted history only)
    items = [r for r in all_items if r.get("type") != "run"]

    # Cron and runner status
    try:
        present, cron_line = cron_status(getattr(args, "repo", "."))
    except Exception:
        present, cron_line = False, ""
    rstat = runner_status()

    # Heartbeat heuristic: consider healthy if a run finished within 3 minutes
    last_run_at = all_runs[-1].get("posted_at") if all_runs else None
    heartbeat_ok = False
    if last_run_at:
        try:
            last_dt = datetime.fromisoformat(last_run_at)
            delta = now_utc() - last_dt  # type: ignore
            heartbeat_ok = delta.total_seconds() <= 180
        except Exception:
            heartbeat_ok = False

    if args.json:
        # Also include pending/failed from schedule in JSON mode
        sched = list_jobs(rsince)
        for j in sched:
            if j.get("status") in ("pending", "failed"):
                items.append({
                    "posted_at": j.get("time_utc"),
                    "status": j.get("status"),
                    "source": "scheduled",
                    "tweet_id": j.get("posted_tweet_id"),
                    "id": j.get("id"),
                    "text": j.get("text"),
                })
        items.sort(key=lambda r: r.get("posted_at", ""))
        out = {
            "cron": {"installed": present, "entry": cron_line},
            "runner": rstat,
            "heartbeat_ok": heartbeat_ok,
            "runs": run_logs,
            "history": items,
        }
        print_json(out)
        return 0

    # Human output header: cron/runner
    print("Status\n" + "\033[2m" + ("─" * 40) + "\033[0m")
    print(f"cron: {'installed' if present else 'not installed'}")
    if not rstat.get("running"):
        print("runner: not running")
        # Show last run heartbeat assessment even if runner is off
        hb = 'yes' if heartbeat_ok else ('no' if last_run_at else 'unknown')
        print(f"heartbeat: {hb}")
    else:
        started = rstat.get('started_at')
        hbts = rstat.get('last_heartbeat')
        started_s = iso_utc_to_local_hms(started, args.tz) if started else ''
        hb_s = iso_utc_to_local_hms(hbts, args.tz) if hbts else ''
        print(f"runner: running pid={rstat.get('pid')} started_at={started_s} last_heartbeat={hb_s}")
        # When running, use last_heartbeat freshness (<= 90s) as health
        hb_ok = False
        try:
            lh = rstat.get('last_heartbeat')
            if lh:
                last_hb = datetime.fromisoformat(lh)
                hb_ok = (now_utc() - last_hb).total_seconds() <= 90
        except Exception:
            hb_ok = False
        print(f"heartbeat: {'yes' if hb_ok else 'no'}")
    if last_run_at:
        print(f"last_run: {iso_utc_to_local_hms(last_run_at, args.tz)}")

    # Recent runs summary (exclude skips)
    if run_logs:
        print("\nRecent runs\n" + "\033[2m" + ("─" * 40) + "\033[0m")
        for r in run_logs[-10:]:  # show last 10
            when = iso_utc_to_local_hms(r.get("posted_at", ""), args.tz) if r.get("posted_at") else ""
            msg = r.get("message") or ""
            print(f"{when} | {msg}")

    # Human output: combine posted and pending/failed
    sched = list_jobs(rsince)
    for j in sched:
        if j.get("status") in ("pending", "failed"):
            items.append({
                "posted_at": j.get("time_utc"),
                "status": j.get("status"),
                "source": "scheduled",
                "tweet_id": j.get("posted_tweet_id"),
                "id": j.get("id"),
                "text": j.get("text"),
            })
    items.sort(key=lambda r: r.get("posted_at", ""))
    print("\nHistory\n" + "\033[2m" + ("─" * 40) + "\033[0m")
    print(format_journal_table(items, tz=args.tz))

    # Prime time coverage at the bottom
    print("\nPrime Time Coverage (next 10 days)\n" + "\033[2m" + ("─" * 40) + "\033[0m")
    _print_prime_time_coverage()
    return 0


def humanize_delta(total_seconds: int) -> str:
    if total_seconds <= 0:
        return "now"
    minutes, sec = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days} day" + ("s" if days != 1 else ""))
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if not days and minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    if not parts:
        parts.append("<1 minute")
    return "in " + " ".join(parts)


def cmd_run_once(args: argparse.Namespace) -> int:
    res = run_once(max_attempts_per_post=args.max_retries)
    if not res.get("ok"):
        if res.get("reason") == "runner_active":
            print("runner already active", file=sys.stderr)
            return 2
        print_json(res)
        return 1
    if args.json:
        print_json(res)
    else:
        print(f"checked={res.get('checked')} posted={len(res.get('posted', []))} failed={len(res.get('failed', []))}")
        # Per-item details for cron log visibility
        for jid in res.get('posted', []):
            j = get_job(jid)
            tid = j.get('posted_tweet_id') if j else None
            url = f"https://x.com/i/web/status/{tid}" if tid else ""
            text = (j.get('text') or '') if j else ''
            snippet = _text_snippet(text, width=120)
            line = f"posted id={jid} url={url} text={snippet}"
            if _use_color():
                line = f"\033[32m{line}\033[0m"
            print(line)
        for jid in res.get('failed', []):
            j = get_job(jid)
            err = j.get('last_error') if j else 'unknown error'
            text = (j.get('text') or '') if j else ''
            snippet = _text_snippet(text, width=120)
            line = f"failed id={jid} error={err} text={snippet}"
            if _use_color():
                line = f"\033[31m{line}\033[0m"
            print(line)
    return 0


def cmd_runner_status(args: argparse.Namespace) -> int:
    s = runner_status()
    if args.json:
        print_json(s)
    else:
        if not s.get("running"):
            print("runner: not running")
        else:
            print(f"runner: running pid={s.get('pid')} started_at={s.get('started_at')} last_heartbeat={s.get('last_heartbeat')}")
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    if not args.text:
        print("--text is required", file=sys.stderr)
        return 2
    if args.json and not args.yes:
        print("--json mode requires --yes for post confirmation", file=sys.stderr)
        return 2
    if not args.yes:
        print("\033[1m\033[36mAbout to post:\033[0m")
        print("\033[2m" + ("─" * 40) + "\033[0m")
        print(args.text)
        print("\033[2m" + ("─" * 40) + "\033[0m")
        if not confirm("Proceed to post?", default=False):
            print("\033[33maborted\033[0m")
            return 0
    try:
        tid, raw = post_tweet(args.text, max_attempts=args.max_retries)
    except ApiError as e:
        if args.json:
            print_json({"ok": False, "error": str(e)})
        else:
            print(f"\033[31mpost failed: {e}\033[0m", file=sys.stderr)
        return 1
    if args.json:
        print_json({"ok": True, "tweet_id": tid, "url": f"https://x.com/i/web/status/{tid}", "raw": raw})
    else:
        # Add a separator so multi-line input above is visually distinct
        print("\n" + "\033[2m" + ("─" * 40) + "\033[0m" + "  \033[1mResult\033[0m")
        print(f"\033[32mposted: https://x.com/i/web/status/{tid}\033[0m")
    # Log to journal for full monitoring
    append_journal({
        "id": gen_id(),
        "idempotency_key": None,
        "tweet_id": tid,
        "posted_at": now_utc().isoformat(),
        "source": "immediate",
        "text": args.text,
    })
    return 0


def cmd_cron_on(args: argparse.Namespace) -> int:
    ok, entry = cron_on(args.repo)
    if args.json:
        print_json({"ok": ok, "entry": entry})
    else:
        print(f"cron installed: {entry}")
    return 0


def cmd_cron_off(args: argparse.Namespace) -> int:
    ok, removed = cron_off(args.repo)
    if args.json:
        print_json({"ok": ok, "removed": removed})
    else:
        print(f"cron removed entries: {removed}")
    return 0


def cmd_cron_status(args: argparse.Namespace) -> int:
    present, line = cron_status(args.repo)
    if args.json:
        print_json({"present": present, "line": line})
    else:
        if present:
            print(f"cron: present -> {line}")
        else:
            print("cron: not installed")
    return 0


def cmd_auth_check(args: argparse.Namespace) -> int:
    status = auth_status()
    if args.json:
        print_json(status)
        return 0
    print("Auth configuration\n" + "\033[2m" + ("─" * 40) + "\033[0m")
    print(f"endpoint: {status['endpoint']}")
    print("oauth1:")
    for k, present in status["oauth1"].items():
        mark = "\033[32m✓\033[0m" if present else "\033[31m✗\033[0m"
        print(f"  {k}: {mark}")
    print(f"oauth1_complete: {'\033[32mtrue\033[0m' if status['oauth1_complete'] else '\033[31mfalse\033[0m'}")
    print(f"bearer_present: {'yes' if status['bearer_present'] else 'no'}")
    print(f"oauth2_client_present: {'yes' if status['oauth2_client_present'] else 'no'}")
    if status.get("notes"):
        print("notes:")
        for n in status["notes"]:
            print(f"  - {n}")
    return 0


def _now_local_iso(tz_name: str | None) -> str:
    try:
        return iso_utc_to_local_hms(now_utc().isoformat(), tz_name)
    except Exception:
        return now_utc().strftime("%Y-%m-%d %H:%M:%S")


def _print_with_ts(line: str, tz_name: str | None) -> None:
    def _colorize(s: str) -> str:
        if not _use_color():
            return s
        ls = s.lstrip()
        if ls.startswith("failed id="):
            return f"\033[31m{s.rstrip()}\033[0m"  # red
        if ls.startswith("posted id="):
            return f"\033[32m{s.rstrip()}\033[0m"  # green
        return s.rstrip()
    ts = _now_local_iso(tz_name)
    print(f"{ts} | {_colorize(line)}\n", end="")


def _tail_file_follow(path: str, lines: int, tz_name: str | None, lookback_min: int) -> int:
    # Wait for file to appear
    while not os.path.exists(path):
        _print_with_ts(f"waiting for log file at {path}...", tz_name)
        time.sleep(2)
    # Print last N lines only when not using lookback filtering
    if lookback_min <= 0 and lines > 0:
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.readlines()
        except Exception as e:
            print(f"\033[31mfailed to read log: {e}\033[0m", file=sys.stderr)
            return 1
        for line in content[-lines:]:
            _print_with_ts(line, tz_name)

    # Follow
    def _get_inode(p: str) -> int:
        try:
            return os.stat(p).st_ino
        except Exception:
            return -1
    inode = _get_inode(path)
    try:
        f = open(path, 'r', encoding='utf-8', errors='replace')
        f.seek(0, os.SEEK_END)
    except Exception as e:
        print(f"\033[31mfailed to open log for follow: {e}\033[0m", file=sys.stderr)
        return 1
    try:
        while True:
            where = f.tell()
            line = f.readline()
            if line:
                _print_with_ts(line, tz_name)
                continue
            # rotation detection
            cur_inode = _get_inode(path)
            if cur_inode != inode and cur_inode != -1:
                try:
                    f.close()
                except Exception:
                    pass
                try:
                    f = open(path, 'r', encoding='utf-8', errors='replace')
                    inode = cur_inode
                    _print_with_ts("log rotated; reopening from start", tz_name)
                except Exception:
                    time.sleep(1)
                    continue
            else:
                time.sleep(0.5)
                f.seek(where)
    except KeyboardInterrupt:
        try:
            f.close()
        except Exception:
            pass
        return 0


def cmd_logs_follow(args: argparse.Namespace) -> int:
    # Show recent run summaries first, based on journal, then follow raw log
    lookback = int(getattr(args, 'lookback', 10) or 0)
    if lookback > 0:
        # Compute since and show runs
        from .util import now_utc as _now, timedelta as _td
        since_iso = (_now() - _td(minutes=lookback)).isoformat()
        runs = [r for r in read_journal(since_iso) if r.get('type') == 'run' and not r.get('skipped')]
        for r in runs:
            when = r.get('posted_at') or r.get('started_at')
            when_s = iso_utc_to_local_hms(when, args.tz) if when else _now_local_iso(args.tz)
            msg = r.get('message') or ''
            line = f"run: {msg}"
            if _use_color():
                if (r.get('failed_count') or 0) > 0:
                    line = f"\033[31m{line}\033[0m"
                else:
                    line = f"\033[32m{line}\033[0m"
            print(f"{when_s} | {line}")
    return _tail_file_follow(args.path, args.lines, args.tz, lookback)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="x",
        description=(
            "x-cli: schedule one-off X posts locally and publish them at the right time.\n"
            "Uses OAuth 1.0a keys from .env; supports safe run-once with cron."
        ),
        epilog=(
            "Examples:\n"
            "  x schedule --text 'Hello' --at '2025-09-14 21:00' --tz HKT\n"
            "  x schedule --text 'Hello EU' --at 'EU morning'\n"
            "  x schedule --text 'Hello in 2d noon' --at '2d NY noon'\n"
            "  x update <id> --at '2025-09-14 22:30'\n"
            "  x remove <id>\n"
            "  x monitor\n"
            "  x post --text 'Ship it'\n"
            "  x run-once\n"
            "  x cron on --repo .\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # schedule
    ps = sub.add_parser(
        "schedule",
        help="manage scheduled posts (assign, monitor, update, remove)",
        description=(
            "Schedule one-off posts and manage them. Times default to HKT unless --tz is set.\n"
            "--at supports ISO ('YYYY-MM-DD HH:MM'), shorthand 'HH:MM' (next occurrence), 'Nd ...' day offsets,\n"
            "and prime-time keywords like 'EU morning' (random time within that window).\n"
            "Default action: 'assign' when omitted."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    # If no action is provided, default to 'assign' so `x schedule` behaves like `x schedule assign`.
    ps.add_argument("action", nargs="?", choices=["assign", "monitor", "update", "remove"], default="assign")
    ps.add_argument("--text", help="post text content (required for assign; optional for update)")
    ps.add_argument("--at", help="scheduled time (ISO8601, 'YYYY-MM-DD HH:MM', or 'HH:MM' for next occurrence)")
    ps.add_argument("--tz", help="IANA timezone name (default: HKT)")
    ps.add_argument("--id", help="job id for monitor/update/remove")
    ps.add_argument("--since", help="filter monitor list to items scheduled at/after this time")
    ps.add_argument("--json", action="store_true", help="output JSON instead of human-readable format")
    ps.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompts (assign)")
    ps.set_defaults(func=cmd_schedule)

    # run-once
    pr = sub.add_parser(
        "run-once",
        help="post all due items now and exit",
        description=(
            "Checks the local schedule and posts any items whose time <= now, then exits.\n"
            "Use with cron/systemd for periodic processing. Uses a PID lock to avoid overlap."
        ),
    )
    pr.add_argument("--max-retries", type=int, default=2, help="max attempts per post within this run (default: 2)")
    pr.add_argument("--json", action="store_true", help="output JSON result")
    pr.set_defaults(func=cmd_run_once)

    # runner status
    prs = sub.add_parser(
        "runner",
        help="runner controls",
        description="Show runner status (ephemeral; run-once holds the lock briefly while running).",
    )
    prs_sub = prs.add_subparsers(dest="runner_cmd", required=True)
    prs_status = prs_sub.add_parser("status", help="show runner status")
    prs_status.add_argument("--json", action="store_true", help="output JSON status")
    prs_status.set_defaults(func=cmd_runner_status)

    # immediate post
    pp = sub.add_parser(
        "post",
        help="post immediately (bypass scheduler)",
        description="Create a tweet now using your OAuth 1.0a credentials from .env.",
    )
    pp.add_argument("--text", help="post text content (required)")
    pp.add_argument("--max-retries", type=int, default=2, help="max attempts within this command (default: 2)")
    pp.add_argument("--json", action="store_true", help="output JSON response")
    pp.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompt")
    pp.set_defaults(func=cmd_post)

    # cron controls
    pc = sub.add_parser(
        "cron",
        help="manage crontab for run-once",
        description=(
            "Install/remove a per-minute cron entry that runs 'x run-once'.\n"
            "The entry cd's into your repo so .env is picked up."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    pc_sub = pc.add_subparsers(dest="cron_cmd", required=True)

    pc_on = pc_sub.add_parser("on", help="install per-minute run-once cron job")
    pc_on.add_argument("--repo", default=".", help="path to repo root containing bin/x (default: .)")
    pc_on.add_argument("--json", action="store_true", help="output JSON result")
    pc_on.set_defaults(func=cmd_cron_on)

    pc_off = pc_sub.add_parser("off", help="remove run-once cron job")
    pc_off.add_argument("--repo", default=".", help="path to repo root (default: .)")
    pc_off.add_argument("--json", action="store_true", help="output JSON result")
    pc_off.set_defaults(func=cmd_cron_off)

    pc_status = pc_sub.add_parser("status", help="show if cron job is installed")
    pc_status.add_argument("--repo", default=".", help="path to repo root (default: .)")
    pc_status.add_argument("--json", action="store_true", help="output JSON result")
    pc_status.set_defaults(func=cmd_cron_status)

    # auth utilities
    pa = sub.add_parser(
        "auth",
        help="auth utilities",
        description="Show which credentials are loaded and common issues.",
    )
    pa_sub = pa.add_subparsers(dest="auth_cmd", required=True)
    pa_check = pa_sub.add_parser("check", help="inspect loaded credentials")
    pa_check.add_argument("--json", action="store_true", help="output JSON status")
    pa_check.set_defaults(func=cmd_auth_check)

    # full monitor (journal)
    pm = sub.add_parser(
        "monitor",
        help="show posted history from journal",
        description="Displays posted tweets (both scheduled and immediate) since a given time.",
    )
    pm.add_argument(
        "--since",
        default="1d",
        help="filter to entries with posted_at >= this time (ISO or 'YYYY-MM-DD HH:MM' or relative like 1d, 12h). Default: 1d",
    )
    pm.add_argument("--tz", help="IANA timezone name for display (default: HKT)")
    pm.add_argument("--json", action="store_true", help="output raw JSON entries")
    pm.add_argument("--repo", default=".", help="path to repo root for cron status (default: .)")
    pm.set_defaults(func=cmd_monitor)

    # tweet details
    pt = sub.add_parser(
        "tweet",
        help="show details using internal ID",
        description="Lookup details using the internal ID (from schedule/monitor). If posted, fetches the tweet.",
    )
    pt.add_argument("--id", required=True, help="internal id (from schedule/monitor)")
    pt.add_argument("--json", action="store_true", help="output raw JSON response")
    pt.set_defaults(func=cmd_tweet_show)

    # logs
    pl = sub.add_parser(
        "logs",
        help="log utilities",
        description="Tail the cron process log with timestamps for close monitoring.",
    )
    pl_sub = pl.add_subparsers(dest="logs_cmd", required=True)
    pl_follow = pl_sub.add_parser("follow", help="tail -f cron log with timestamps")
    pl_follow.add_argument("--path", default=cron_log_default_path(), help="path to cron log (default: ~/.x-cli/cron.log)")
    pl_follow.add_argument("--lines", type=int, default=50, help="show last N lines before following (default: 50)")
    pl_follow.add_argument("--lookback", type=int, default=10, help="minutes to look back for recent runs (default: 10)")
    pl_follow.add_argument("--tz", help="IANA timezone for timestamps (default: HKT)")
    pl_follow.set_defaults(func=cmd_logs_follow)

    # ai utilities
    pai = sub.add_parser(
        "ai",
        help="AI utilities",
        description="AI helpers like proofreading drafts before posting.",
    )
    pai_sub = pai.add_subparsers(dest="ai_cmd", required=True)
    pai_pf = pai_sub.add_parser(
        "proofread",
        help="proofread a draft for X",
        description=(
            "Proofread and punch up a draft post while keeping its structure/style. "
            "Never uses em dashes. Highlights length relative to X limits."
        ),
    )
    pai_pf.add_argument("text_parts", nargs="*", help="draft text as positional words (join with spaces)")
    pai_pf.add_argument("--text", help="draft text (overrides positional); omit to read from stdin")
    pai_pf.add_argument("--model", default="gpt-5-mini", help="LLM model (default: gpt-5-mini)")
    pai_pf.add_argument("--json", action="store_true", help="output JSON instead of formatted text")
    pai_pf.set_defaults(func=cmd_ai_proofread)

    # short alias: draft
    pd = sub.add_parser(
        "draft",
        help="proofread a draft (alias of ai proofread)",
        description="Short alias for 'ai proofread' to punch up a draft.",
    )
    pd.add_argument("text_parts", nargs="*", help="draft text as positional words (join with spaces)")
    pd.add_argument("--text", help="draft text (overrides positional); omit to read from stdin")
    pd.add_argument("--model", default="gpt-5-mini", help="LLM model (default: gpt-5-mini)")
    pd.add_argument("--json", action="store_true", help="output JSON instead of formatted text")
    pd.set_defaults(func=cmd_ai_proofread)

    # simple aliases: remove / update at top-level
    prmv = sub.add_parser(
        "remove",
        help="remove a scheduled job (alias of 'schedule remove')",
        description="Remove a scheduled job by id.",
    )
    prmv.add_argument("id", help="job id to remove")
    prmv.add_argument("--json", action="store_true", help="output JSON result")
    prmv.set_defaults(func=cmd_remove_simple)

    pupd = sub.add_parser(
        "update",
        help="update a scheduled job (alias of 'schedule update')",
        description="Update a job's time and/or text by id.",
    )
    pupd.add_argument("id", help="job id to update")
    pupd.add_argument("--at", help="new scheduled time (ISO, 'YYYY-MM-DD HH:MM', or 'HH:MM')")
    pupd.add_argument("--text", help="new text (omit to keep current)")
    pupd.add_argument("--tz", help="IANA timezone name for interpreting --at (default: HKT)")
    pupd.add_argument("--json", action="store_true", help="output JSON result")
    pupd.set_defaults(func=cmd_update_simple)

    # details: show full text and when for a given internal id
    pdet = sub.add_parser(
        "detail",
        help="show full details for an internal id",
        description=(
            "Show full text and timing for a scheduled/posted item by internal id. "
            "Uses schedule first, then falls back to journal."
        ),
    )
    pdet.add_argument("id", help="internal id from schedule/monitor")
    pdet.add_argument("--tz", help="IANA timezone for display (default: HKT)")
    pdet.add_argument("--json", action="store_true", help="output JSON details")
    pdet.set_defaults(func=cmd_detail)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def cmd_tweet_show(args: argparse.Namespace) -> int:
    # Internal ID: try schedule first, then journal
    j = get_job(args.id)
    tweet_id = None
    source = "scheduled"
    if j:
        tweet_id = j.get("posted_tweet_id")
        if not tweet_id:
            msg = "not posted yet" if j.get("status") == "pending" else j.get("last_error") or "no tweet id"
            print(f"\033[33m{msg}\033[0m")
            if args.json:
                print_json(j)
            return 0
    else:
        rec = journal_find_by_id(args.id)
        if not rec:
            print("\033[31mnot found\033[0m", file=sys.stderr)
            return 1
        tweet_id = rec.get("tweet_id")
        source = rec.get("source", "")
    if not tweet_id:
        print("\033[31mno tweet id available\033[0m", file=sys.stderr)
        return 1
    try:
        data = get_tweet(tweet_id)
    except ApiError as e:
        if args.json:
            print_json({"ok": False, "error": str(e)})
        else:
            print(f"\033[31mlookup failed: {e}\033[0m", file=sys.stderr)
        return 1
    if args.json:
        print_json(data)
        return 0
    d = data.get("data", {}) if isinstance(data, dict) else {}
    tid = d.get("id", tweet_id)
    text = d.get("text", "")
    print(f"Internal ID: {args.id}  Source: {source}")
    print(f"Tweet ID: {tid}")
    print(f"URL: https://x.com/i/web/status/{tid}")
    print("Text:\n" + text)
    return 0


def cmd_remove_simple(args: argparse.Namespace) -> int:
    ok = remove_job(args.id)
    if args.json:
        print_json({"ok": ok, "id": args.id})
    else:
        print("\033[32mremoved\033[0m" if ok else "\033[31mjob not found\033[0m")
    return 0 if ok else 1


def cmd_update_simple(args: argparse.Namespace) -> int:
    if not args.at and not args.text:
        print("--at or --text is required", file=sys.stderr)
        return 2
    try:
        j = update_job(args.id, text=args.text, at=args.at, tz_name=args.tz)
    except KeyError as e:
        if args.json:
            print_json({"ok": False, "error": str(e)})
        else:
            print(str(e), file=sys.stderr)
        return 1
    if args.json:
        print_json(j)
    else:
        print(f"\033[32mupdated: id={j['id']} at={j['time_utc']} tz={j['tz']}\033[0m")
    return 0


def cmd_detail(args: argparse.Namespace) -> int:
    tz = args.tz or "HKT"
    tzinfo = default_tz_from_name(tz)
    # Try schedule first
    j = get_job(args.id)
    if j:
        # compute when/length
        time_utc = j.get("time_utc")
        time_local = iso_utc_to_local_hms(time_utc, tz) if time_utc else None
        rel = None
        if time_utc:
            dt = datetime.fromisoformat(time_utc).astimezone(tzinfo)
            now_l = datetime.now(tzinfo)
            delta = int((dt - now_l).total_seconds())
            if delta >= 0:
                rel = humanize_delta(delta)
            else:
                rel = humanize_delta(abs(delta)).replace("in ", "") + " ago"
        text = j.get("text") or ""
        words = len(text.split())
        chars = len(text)
        out = {
            "id": j.get("id"),
            "status": j.get("status"),
            "at": time_local,
            "when": rel,
            "tz": tz,
            "text": text,
            "tweet_id": j.get("posted_tweet_id"),
            "words": words,
            "chars": chars,
        }
        if args.json:
            print_json(out)
        else:
            print(f"ID: {out['id']}  Status: {out['status']}")
            if out["at"]:
                print(f"at({tz}): {out['at']}")
            if out["when"]:
                print(f"when: {out['when']}")
            # length with coloring
            cc = f"{chars}"
            if _use_color():
                cc = f"\033[31m{chars}\033[0m" if chars > 280 else f"\033[32m{chars}\033[0m"
            print(f"length: words={words} chars={cc}")
            if out.get("tweet_id"):
                print(f"URL: https://x.com/i/web/status/{out['tweet_id']}")
            print("\033[2m" + ("─" * 40) + "\033[0m")
            print(text)
        return 0
    # Fallback to journal
    rec = journal_find_by_id(args.id)
    if not rec:
        print("\033[31mnot found\033[0m", file=sys.stderr)
        return 1
    when_local = iso_utc_to_local_hms(rec.get("posted_at", ""), tz) if rec.get("posted_at") else None
    # compute relative and length
    rel = None
    if rec.get("posted_at"):
        dt = datetime.fromisoformat(rec.get("posted_at")).astimezone(tzinfo)  # type: ignore
        now_l = datetime.now(tzinfo)
        delta = int((dt - now_l).total_seconds())
        if delta >= 0:
            rel = humanize_delta(delta)
        else:
            rel = humanize_delta(abs(delta)).replace("in ", "") + " ago"
    text = rec.get("text") or ""
    words = len(text.split())
    chars = len(text)
    out = {
        "id": rec.get("id"),
        "status": rec.get("status", "posted"),
        "at": when_local,
        "when": rel,
        "tz": tz,
        "text": rec.get("text"),
        "tweet_id": rec.get("tweet_id"),
        "source": rec.get("source"),
        "words": words,
        "chars": chars,
    }
    if args.json:
        print_json(out)
    else:
        print(f"ID: {out['id']}  Status: {out['status']}  Source: {out.get('source') or ''}")
        if out["at"]:
            print(f"at({tz}): {out['at']}")
        if out["when"]:
            print(f"when: {out['when']}")
        cc = f"{chars}"
        if _use_color():
            cc = f"\033[31m{chars}\033[0m" if chars > 280 else f"\033[32m{chars}\033[0m"
        print(f"length: words={words} chars={cc}")
        if out.get("tweet_id"):
            print(f"URL: https://x.com/i/web/status/{out['tweet_id']}")
        print("\033[2m" + ("─" * 40) + "\033[0m")
        print(text)
    return 0


def cmd_ai_proofread(args: argparse.Namespace) -> int:
    # Gather input text
    # Prefer explicit --text, else join positional parts, else stdin
    draft = args.text
    if (not draft or not draft.strip()) and getattr(args, 'text_parts', None):
        draft = " ".join(args.text_parts).strip()
    if not draft:
        try:
            draft = sys.stdin.read()
        except Exception:
            draft = None
    if not draft or not draft.strip():
        print("--text is required (or provide via stdin)", file=sys.stderr)
        return 2
    draft = draft.strip()

    # Build LLM client
    model = args.model
    llm = LLMClient(model=model)

    system_prompt = (
        "You are a world-class editor for X (Twitter) posts. "
        "The user will provide a draft. Your task:\n"
        "- Correct grammar and fix unnatural expressions.\n"
        "- Make it punchy and concise for X.\n"
        "- Preserve the draft's structure and stylistic feel as much as possible.\n"
        "- Never use em dashes (—); prefer commas, periods, or hyphens instead.\n"
        "Output only the improved text, no commentary."
    )

    try:
        improved = llm.chat(system=system_prompt, user=draft)
    except Exception as e:
        if args.json:
            print_json({"ok": False, "error": str(e)})
        else:
            print(f"\033[31mproofread failed: {e}\033[0m", file=sys.stderr)
        return 1

    text_out = str(improved).strip()
    words = len(text_out.split())
    chars = len(text_out)

    if args.json:
        print_json({"ok": True, "text": text_out, "words": words, "chars": chars, "model": model})
        return 0

    # Pretty output
    print("\033[1m\033[36mProofread Draft\033[0m")
    print("\033[2m" + ("─" * 40) + "\033[0m")
    print(text_out)
    print("\033[2m" + ("─" * 40) + "\033[0m")
    wc_str = f"words={words} chars={chars}"
    if _use_color():
        if chars > 280:
            wc_str = f"words={words} chars=\033[31m{chars}\033[0m"
        else:
            wc_str = f"words={words} chars=\033[32m{chars}\033[0m"
    print(wc_str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
