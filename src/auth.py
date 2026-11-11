"""Where the login token lives and how to read its expiry.

The site logs you in through your institution's SSO in the browser and then
keeps two values in ``localStorage``: ``token`` (a JWT, valid ~5 hours) and
``refreshToken`` (used to get a new token without logging in again).
We store a copy of both in a small JSON file with 0600 permissions.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

DEFAULT_AUTH_FILE = config.CONFIG_DIR / "auth.json"

# Paste this into the browser DevTools console on the venue site after logging in.
BROWSER_SNIPPET = (
    "copy(JSON.stringify({token: JSON.parse(localStorage.getItem('token')), "
    "refreshToken: JSON.parse(localStorage.getItem('refreshToken') || '\"\"')}))"
)


def auth_file() -> Path:
    return Path(os.environ.get("VB_AUTH_FILE", DEFAULT_AUTH_FILE))


@dataclass
class Auth:
    token: str
    refresh_token: str = ""

    @property
    def expires_at(self) -> datetime | None:
        return jwt_expiry(self.token)

    @property
    def username(self) -> str:
        return str(jwt_claims(self.token).get("username", ""))


def jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


def jwt_expiry(token: str) -> datetime | None:
    exp = jwt_claims(token).get("exp")
    return datetime.fromtimestamp(exp, tz=timezone.utc) if exp else None


def parse_pasted(text: str) -> Auth:
    """Accept the JSON from :data:`BROWSER_SNIPPET` or a bare JWT."""
    text = text.strip()
    if text.startswith("{"):
        data = json.loads(text)
        return Auth(token=data["token"].strip(), refresh_token=(data.get("refreshToken") or "").strip())
    return Auth(token=text.strip('"'))


def load() -> Auth | None:
    path = auth_file()
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return Auth(token=data["token"], refresh_token=data.get("refreshToken", ""))


def save(auth: Auth) -> Path:
    path = auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"token": auth.token, "refreshToken": auth.refresh_token}, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)
    return path
