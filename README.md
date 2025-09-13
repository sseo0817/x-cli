# X CLI (Personal Scheduler)

Minimal Python + Bash CLI to schedule one-off posts to X (Twitter) using the v2 API (free tier). Since X does not expose scheduled posts in the free API, scheduling is handled locally via a JSON store and a `run-once` runner.

## Features

- Schedule one-off posts at a specific local time (default HKT).
- Safe runner with PID lock to avoid concurrent runs.
- Idempotency via a journal to prevent duplicate posts.
- Monitor scheduled items (human-friendly table or JSON).
- Update or remove scheduled items.
- Immediate `post` command for quick tweets.

## Requirements

- Python 3.9+
- `requests`, `python-dateutil`, `python-dotenv`, `requests-oauthlib` (see `requirements.txt`).
- OAuth 1.0a credentials in `.env`: `API_KEY`, `API_SECRET`, `ACCESS_TOKEN`, `ACCESS_TOKEN_SECRET` (required to post).

## Install

```
# Create venv (example using uv)
uv venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Make the CLI available (optional PATH update)
chmod +x bin/x
export PATH="$PWD/bin:$PATH"  # optional; see alternatives below

# Put your credentials in .env
cat > .env << 'EOF'
API_KEY=your_api_key
API_SECRET=your_api_secret
ACCESS_TOKEN=your_access_token
ACCESS_TOKEN_SECRET=your_access_token_secret
# Optional read-only app token (not used for posting):
X_BEARER_TOKEN=
EOF
```

The CLI automatically loads `.env` from the current directory using python-dotenv. No need to export variables.

## Usage

### Schedule a post

```
x schedule assign --text "Hello from scheduler" --at "2025-09-14 21:00" --tz HKT
```

Notes:
- `--at` accepts ISO8601 or `YYYY-MM-DD HH:MM` in the provided timezone (default HKT).
- Time is normalized to UTC for storage.

### Monitor scheduled items

```
x schedule monitor --since "2025-09-14"
x schedule monitor --id <job_id>
```

Add `--json` for machine-readable output.

### Update or remove

```
x schedule update --id <job_id> --at "2025-09-14 22:30" --tz HKT
x schedule update --id <job_id> --text "New content"
x schedule remove --id <job_id>
```

### Post immediately

```
x post --text "Ship it" --max-retries 2
```

### Process due items once

```
x run-once --max-retries 2
```

This acquires a lock at `~/.x-cli/runner.lock`. If another runner is active, it exits with code 2.

### Check runner status

```
x runner status
```

Reports whether a runner is currently active.

## Cron/Systemd Example

Run once per minute to process due jobs (uses .env in repo and auto-activates .venv/ if present):

```
* * * * * cd /absolute/path/to/repo && VENV_ACT="/absolute/path/to/repo/.venv/bin/activate"; [ -f "$VENV_ACT" ] || VENV_ACT="/absolute/path/to/repo/venv/bin/activate"; [ -f "$VENV_ACT" ] && . "$VENV_ACT"; /absolute/path/to/repo/bin/x run-once >> $HOME/.x-cli/cron.log 2>&1
```

This pattern is safe: if a previous run is still active, the new one exits immediately due to the lock.

## Data Files

- `~/.x-cli/schedule.json` — scheduled jobs.
- `~/.x-cli/journal.jsonl` — append-only log of posted tweets (for idempotency).
- `~/.x-cli/runner.lock` — lock file while the runner is active.

## Limits and Notes

- Free v2 tier supports text-only posting with user-auth; media and advanced features require elevated tiers.
- Retries are capped at 2. It is safer to miss than to double post.
- The runner is designed to be short-lived (`run-once`) and triggered by cron/systemd.

## Credentials Explained

- API_KEY and API_SECRET: your app’s consumer key/secret.
- ACCESS_TOKEN and ACCESS_TOKEN_SECRET: user-context credentials (tied to your X account) enabling write actions.
- X_BEARER_TOKEN: app-only token useful for read-only endpoints; not used to post tweets.

## Development

Project layout:

- `bin/x` — bash entrypoint.
- `xcli/cli.py` — argparse CLI.
- `xcli/schedule.py` — schedule CRUD.
- `xcli/runner.py` — run-once processor, locking, journal.
- `xcli/api.py` — X API client.
- `xcli/util.py` — helpers (paths, time, atomic IO).

## Using the CLI without editing PATH

- Run with a relative path:
  - `./bin/x schedule assign --text "..." --at "2025-09-14 21:00"`
- Or use Python directly:
  - `python -m xcli.cli schedule monitor --since "2025-09-14"`
- Or create a one-time alias (shell session only):
  - `alias x="$PWD/bin/x"`

## Managing Cron from the CLI

- Turn on per-minute run-once:
  - `x cron on --repo /absolute/path/to/repo`
- Turn off:
  - `x cron off --repo /absolute/path/to/repo`
- Status:
  - `x cron status --repo /absolute/path/to/repo`

These commands manage a tagged crontab entry (comment `# x-cli: run-once`). They operate on your user crontab and are safe to run multiple times. If a virtualenv exists at `.venv/` or `venv/` under the repo, it is activated automatically.

## Auth Utilities

- Inspect loaded credentials:
  - `x auth check`
  - `x auth check --json`
  
Shows presence of OAuth 1.0a keys (required for posting), optional Bearer token and OAuth2 client info, and guidance notes.

## Monitor

- `x monitor` shows:
  - Cron install status (installed/not installed).
  - Runner status and a heartbeat check (healthy if a run finished within ~3 minutes).
  - Recent run summaries (success/failure counts), excluding pure "skip" runs where there was nothing due.
  - Use `--repo` to specify which repo path to check cron status against.
  - Timestamps are displayed as `YYYY-MM-DD HH:MM:SS` in your chosen timezone.

## Logs

- Follow cron-run output with timestamps:
  - `x logs follow`
  - Options:
    - `--path` to specify a different log file (default: `~/.x-cli/cron.log`)
    - `--lines` to show the last N lines before following (default: 50)
    - `--lookback` minutes to show recent run summaries before following (default: 10)
    - `--tz` to set the display timezone for timestamps (default: HKT)
  - Each line is prefixed with a timestamp `YYYY-MM-DD HH:MM:SS`.
  - Posted lines are green; failed lines are red.

Notes:
- `x run-once` prints per-item results, which appear in the log:
  - `posted id=... url=... text=...`
  - `failed id=... error=... text=...`
