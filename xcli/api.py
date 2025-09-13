import os
import time
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv, find_dotenv
from requests_oauthlib import OAuth1


X_TWEET_ENDPOINT = "https://api.x.com/2/tweets"
_ENV_LOADED = False


class ApiError(Exception):
    def __init__(self, status: int, body: Any, message: str | None = None):
        msg = message or summarize_error(body) or str(body)
        super().__init__(f"API error {status}: {msg}")
        self.status = status
        self.body = body


def _load_env_once() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        # Load from nearest .env (searching up from cwd) if present
        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path, override=False)
        _ENV_LOADED = True


def get_oauth1_credentials() -> Optional[Tuple[str, str, str, str]]:
    _load_env_once()
    api_key = os.environ.get("API_KEY", "").strip()
    api_secret = os.environ.get("API_SECRET", "").strip()
    access_token = os.environ.get("ACCESS_TOKEN", "").strip()
    access_secret = os.environ.get("ACCESS_TOKEN_SECRET", "").strip()
    if api_key and api_secret and access_token and access_secret:
        return api_key, api_secret, access_token, access_secret
    return None


def get_bearer_token_optional() -> Optional[str]:
    _load_env_once()
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    return token or None


def get_oauth2_client_optional() -> Optional[Tuple[str, str]]:
    _load_env_once()
    cid = os.environ.get("CLIENT_ID", "").strip()
    csec = os.environ.get("CLIENT_SECRET", "").strip()
    if cid and csec:
        return cid, csec
    return None


def auth_status() -> Dict[str, Any]:
    """Return a summary of loaded auth-related env vars.

    This does not perform a network call; it only reports presence/shape.
    """
    _load_env_once()
    api_key = os.environ.get("API_KEY", "").strip()
    api_secret = os.environ.get("API_SECRET", "").strip()
    access_token = os.environ.get("ACCESS_TOKEN", "").strip()
    access_secret = os.environ.get("ACCESS_TOKEN_SECRET", "").strip()

    oauth1 = {
        "API_KEY": bool(api_key),
        "API_SECRET": bool(api_secret),
        "ACCESS_TOKEN": bool(access_token),
        "ACCESS_TOKEN_SECRET": bool(access_secret),
    }
    oauth1_complete = all(oauth1.values())

    bearer = get_bearer_token_optional()
    oauth2_client = get_oauth2_client_optional()

    notes: list[str] = []
    if not oauth1_complete:
        notes.append("OAuth 1.0a keys incomplete; posting will fail.")
    else:
        notes.append("OAuth 1.0a keys present; posting possible if app has write.")
    if bearer:
        notes.append("Bearer token present (useful for GET, not used for posting).")
    if oauth2_client:
        notes.append("OAuth 2.0 client id/secret present (not required for this CLI).")
    notes.append("Posting typically requires a paid plan and 'Read and write' app permissions.")

    return {
        "endpoint": X_TWEET_ENDPOINT,
        "oauth1": oauth1,
        "oauth1_complete": oauth1_complete,
        "bearer_present": bool(bearer),
        "oauth2_client_present": bool(oauth2_client),
        "notes": notes,
    }


def post_tweet(text: str, max_attempts: int = 2) -> Tuple[str, Dict[str, Any]]:
    creds = get_oauth1_credentials()
    if not creds:
        # Provide a clearer guidance if only bearer token is present
        bearer = get_bearer_token_optional()
        if bearer:
            raise RuntimeError("Posting requires user auth via OAuth 1.0a (API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET). App-only Bearer token is read-only for posting.")
        raise RuntimeError("Missing OAuth 1.0a credentials: set API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET in .env")

    api_key, api_secret, access_token, access_secret = creds
    auth = OAuth1(api_key, api_secret, access_token, access_secret)
    headers = {
        "Content-Type": "application/json",
    }
    payload = {"text": text}
    backoff = 1.0
    last_err: Optional[ApiError] = None

    for attempt in range(1, max_attempts + 1):
        resp = requests.post(X_TWEET_ENDPOINT, headers=headers, json=payload, timeout=30, auth=auth)
        if resp.status_code // 100 == 2:
            data = resp.json()
            tweet_id = data.get("data", {}).get("id")
            if not tweet_id:
                raise ApiError(resp.status_code, data, message="Response missing tweet id")
            return tweet_id, data
        # Non-2xx
        body = safe_json(resp)
        last_err = ApiError(resp.status_code, body)
        # Retry only for transient errors
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
            time.sleep(backoff)
            backoff *= 2
            continue
        break

    assert last_err is not None
    raise last_err


def safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"text": resp.text}


def summarize_error(body: Any) -> str | None:
    # X API errors typically include {"errors":[{"message":"...","detail":"...","title":"..."}]} or similar
    try:
        if isinstance(body, dict):
            errs = body.get("errors") or body.get("detail")
            if isinstance(errs, list) and errs:
                e0 = errs[0]
                if isinstance(e0, dict):
                    return e0.get("detail") or e0.get("message") or e0.get("title")
            if isinstance(errs, str):
                return errs
            # Sometimes {"title":"...","detail":"..."}
            title = body.get("title")
            detail = body.get("detail")
            if title or detail:
                return ": ".join([p for p in [title, detail] if p])
    except Exception:
        return None
    return None


def get_tweet(tweet_id: str) -> Dict[str, Any]:
    creds = get_oauth1_credentials()
    if not creds:
        bearer = get_bearer_token_optional()
        if bearer:
            headers = {"Authorization": f"Bearer {bearer}"}
            resp = requests.get(f"{X_TWEET_ENDPOINT}/{tweet_id}", headers=headers, timeout=30)
        else:
            raise RuntimeError("Missing credentials for GET tweet: provide OAuth 1.0a keys or X_BEARER_TOKEN")
    else:
        api_key, api_secret, access_token, access_secret = creds
        auth = OAuth1(api_key, api_secret, access_token, access_secret)
        resp = requests.get(f"{X_TWEET_ENDPOINT}/{tweet_id}", timeout=30, auth=auth)
    if resp.status_code // 100 == 2:
        return resp.json()
    raise ApiError(resp.status_code, safe_json(resp))
