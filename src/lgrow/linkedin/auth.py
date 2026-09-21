"""LinkedIn OAuth 2.0 (3-legged) — authorise, store, refresh.

Scopes used, both from self-serve products that need no approval:

  w_member_social   Share on LinkedIn                    -> publish posts
  openid, profile   Sign In with LinkedIn (OpenID)       -> read your person id

Token lifetimes matter for scheduling: the access token lasts 60 days and the
refresh token 365. The weekly cron refreshes the former; the latter needs a
fresh interactive login once a year, and `lgrow doctor` warns 30 days out.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from dotenv import load_dotenv

from .. import paths

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"

SCOPES = ("openid", "profile", "w_member_social")
DEFAULT_REDIRECT = "http://localhost:8765/callback"


class LinkedInAuthError(RuntimeError):
    pass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None = None
    access_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    scope: str = ""
    person_urn: str | None = None
    person_name: str | None = None
    obtained_at: datetime | None = None

    def is_access_valid(self, skew_seconds: int = 300) -> bool:
        if not self.access_token:
            return False
        if self.access_expires_at is None:
            return True  # unknown expiry — let the API be the judge
        return datetime.now(timezone.utc) + timedelta(seconds=skew_seconds) < self.access_expires_at

    def to_dict(self) -> dict[str, object]:
        def iso(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt else None

        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "access_expires_at": iso(self.access_expires_at),
            "refresh_expires_at": iso(self.refresh_expires_at),
            "scope": self.scope,
            "person_urn": self.person_urn,
            "person_name": self.person_name,
            "obtained_at": iso(self.obtained_at),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Tokens:
        def parse(value: object) -> datetime | None:
            if not isinstance(value, str) or not value:
                return None
            try:
                dt = datetime.fromisoformat(value)
            except ValueError:
                return None
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=data.get("refresh_token") or None,
            access_expires_at=parse(data.get("access_expires_at")),
            refresh_expires_at=parse(data.get("refresh_expires_at")),
            scope=str(data.get("scope") or ""),
            person_urn=data.get("person_urn") or None,
            person_name=data.get("person_name") or None,
            obtained_at=parse(data.get("obtained_at")),
        )


# ─── credential plumbing ─────────────────────────────────────────────────────


def _load_env() -> None:
    if paths.ENV_PATH.exists():
        load_dotenv(paths.ENV_PATH, override=False)


def app_credentials() -> tuple[str, str, str]:
    """(client_id, client_secret, redirect_uri) from .env or the environment."""
    _load_env()
    client_id = os.environ.get("LINKEDIN_CLIENT_ID", "").strip()
    client_secret = os.environ.get("LINKEDIN_CLIENT_SECRET", "").strip()
    redirect = os.environ.get("LINKEDIN_REDIRECT_URI", DEFAULT_REDIRECT).strip()
    if not client_id or not client_secret:
        raise LinkedInAuthError(
            "LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET are not set.\n"
            "Copy .env.example to .env and fill them in — see the README's\n"
            "'LinkedIn setup' section for how to get them."
        )
    return client_id, client_secret, redirect


def load_tokens() -> Tokens | None:
    if not paths.TOKENS_PATH.exists():
        return None
    try:
        data = json.loads(paths.TOKENS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    tokens = Tokens.from_dict(data)
    return tokens if tokens.access_token else None


def save_tokens(tokens: Tokens) -> None:
    paths.ensure_dirs()
    paths.TOKENS_PATH.write_text(
        json.dumps(tokens.to_dict(), indent=2), encoding="utf-8"
    )
    # Contains a live credential — keep it owner-only.
    os.chmod(paths.TOKENS_PATH, 0o600)


# ─── the authorisation code flow ─────────────────────────────────────────────


class _CallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler capturing ?code= from LinkedIn's redirect."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        type(self).result = {k: v[0] for k, v in params.items()}

        if "code" in type(self).result:
            body = (
                "<h2>Authorised.</h2>"
                "<p>lgrow now has permission to post as you. "
                "You can close this tab and return to the terminal.</p>"
            )
        else:
            err = type(self).result.get("error_description") or type(self).result.get(
                "error", "unknown error"
            )
            body = f"<h2>Authorisation failed</h2><p>{err}</p>"

        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: object) -> None:
        pass  # don't spray HTTP logs over the CLI output


def authorization_url(state: str) -> str:
    client_id, _, redirect = app_credentials()
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "state": state,
            "scope": " ".join(SCOPES),
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def login(open_browser: bool = True, timeout: int = 300) -> Tokens:
    """Run the interactive consent flow and persist the resulting tokens."""
    _, _, redirect = app_credentials()
    parsed = urllib.parse.urlparse(redirect)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80

    state = secrets.token_urlsafe(24)
    url = authorization_url(state)

    _CallbackHandler.result = {}
    try:
        server = HTTPServer((host, port), _CallbackHandler)
    except OSError as exc:
        raise LinkedInAuthError(
            f"Cannot listen on {host}:{port} for the OAuth callback ({exc}).\n"
            "Free that port, or set LINKEDIN_REDIRECT_URI to another one and add\n"
            "the same URL to your app's Auth tab on developer.linkedin.com."
        ) from exc

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Opening LinkedIn for authorisation. If nothing opens, visit:\n")
    print(f"  {url}\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — headless box, the URL above still works
            pass

    thread.join(timeout=timeout)
    server.server_close()

    result = _CallbackHandler.result
    if not result:
        raise LinkedInAuthError(
            f"No callback received within {timeout}s. Re-run `lgrow linkedin login`."
        )
    if "code" not in result:
        raise LinkedInAuthError(
            "LinkedIn refused authorisation: "
            + (result.get("error_description") or result.get("error", "unknown"))
        )
    if result.get("state") != state:
        raise LinkedInAuthError("OAuth state mismatch — aborting for safety.")

    tokens = exchange_code(result["code"])
    tokens = enrich_identity(tokens)
    save_tokens(tokens)
    return tokens


def _parse_token_response(payload: dict) -> Tokens:
    now = datetime.now(timezone.utc)
    access_expires = None
    refresh_expires = None
    if isinstance(payload.get("expires_in"), int):
        access_expires = now + timedelta(seconds=payload["expires_in"])
    if isinstance(payload.get("refresh_token_expires_in"), int):
        refresh_expires = now + timedelta(seconds=payload["refresh_token_expires_in"])
    return Tokens(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        access_expires_at=access_expires,
        refresh_expires_at=refresh_expires,
        scope=payload.get("scope", ""),
        obtained_at=now,
    )


def exchange_code(code: str) -> Tokens:
    client_id, client_secret, redirect = app_credentials()
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise LinkedInAuthError(f"Token exchange failed ({resp.status_code}): {resp.text[:400]}")
    return _parse_token_response(resp.json())


def refresh(tokens: Tokens | None = None) -> Tokens:
    """Exchange the refresh token for a new access token."""
    tokens = tokens or load_tokens()
    if not tokens:
        raise LinkedInAuthError("Not authorised yet — run `lgrow linkedin login`.")
    if not tokens.refresh_token:
        raise LinkedInAuthError(
            "No refresh token stored — run `lgrow linkedin login` again."
        )
    client_id, client_secret, _ = app_credentials()
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise LinkedInAuthError(
            f"Refresh failed ({resp.status_code}): {resp.text[:400]}\n"
            "If the refresh token has expired (365 days), run `lgrow linkedin login`."
        )
    fresh = _parse_token_response(resp.json())
    # LinkedIn may not re-issue these; carry forward what we already know.
    fresh.refresh_token = fresh.refresh_token or tokens.refresh_token
    fresh.refresh_expires_at = fresh.refresh_expires_at or tokens.refresh_expires_at
    fresh.person_urn = tokens.person_urn
    fresh.person_name = tokens.person_name
    save_tokens(fresh)
    return fresh


def enrich_identity(tokens: Tokens) -> Tokens:
    """Fill in the person URN via OpenID userinfo — required to author posts."""
    resp = httpx.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {tokens.access_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise LinkedInAuthError(
            f"Could not read your profile ({resp.status_code}): {resp.text[:300]}\n"
            "Confirm the 'Sign In with LinkedIn using OpenID Connect' product is\n"
            "added to your app, which is what grants the openid+profile scopes."
        )
    data = resp.json()
    sub = data.get("sub")
    if not sub:
        raise LinkedInAuthError(f"userinfo returned no 'sub' field: {data}")
    tokens.person_urn = f"urn:li:person:{sub}"
    tokens.person_name = data.get("name") or data.get("given_name")
    return tokens


def valid_tokens(auto_refresh: bool = True) -> Tokens:
    """Tokens guaranteed usable right now, refreshing if needed."""
    tokens = load_tokens()
    if not tokens:
        raise LinkedInAuthError("Not authorised yet — run `lgrow linkedin login`.")
    if tokens.is_access_valid():
        return tokens
    if not auto_refresh:
        raise LinkedInAuthError("Access token expired — run `lgrow linkedin refresh`.")
    return refresh(tokens)
