"""Thin client for the venue booking API.

The site it talks to (base URL, SSO host, app id, signing key and the venue
ids) comes from the ``[site]`` / ``[venues]`` sections of your local config,
so no particular institution is named in the source. See :mod:`config`.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests

import auth as authmod
import config
from config import Venue  # re-exported for callers
from crypto import captcha_encrypt, captcha_point_json, decrypt_response, sign_params

__all__ = ["ApiError", "AuthError", "Venue", "Captcha", "VenueClient"]

_CAPTCHA_TYPE = "blockPuzzle"

# code / errorCode values the frontend treats as "login expired".
_AUTH_CODES = {300, 301, 401}
_AUTH_ERROR_CODES = {1030002, 1130001, 1130002, 1130008, 1130009, 11300011, 11300012, 11300013}

_BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Language-Set": "CN",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    ),
}


class ApiError(RuntimeError):
    pass


class AuthError(ApiError):
    pass


@dataclass
class Captcha:
    token: str
    secret_key: str
    background_b64: str
    piece_b64: str


def _decode_response(resp: requests.Response) -> Any:
    data = resp.json() if resp.text.lstrip().startswith(("{", "[")) else resp.text
    if isinstance(data, str):
        data = json.loads(decrypt_response(data))
    return data


class VenueClient:
    def __init__(self, auth: authmod.Auth | None, *, persist_auth: bool = True, timeout: float = 10.0,
                 relogin: Callable[[], authmod.Auth | None] | None = None):
        """``relogin`` is called when the token can't be refreshed (e.g. a browser SSO login)."""
        self.auth = auth
        self.persist_auth = persist_auth
        self.relogin = relogin
        self.timeout = timeout
        s = config.site()
        self._base = s.site_base
        self._index = s.index_url
        self.session = requests.Session()
        self.session.headers.update(_BASE_HEADERS)
        self.session.headers.update({"Origin": s.origin, "Referer": s.index_url})
        self._captcha_client_uid = f"slider-{uuid.uuid4()}"

    # ------------------------------------------------------------------ core
    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: Any = None, authed: bool = True, retry_auth: bool = True,
                 api_v2: bool = True, timeout: float | None = None) -> Any:
        headers = {}
        if authed:
            if not self.auth and not (retry_auth and self.renew_login()):
                raise AuthError("Not logged in. Run `venue-booking auth` first.")
            headers["token"] = self.auth.token
        if api_v2:
            headers["x-api-version"] = "2.0.0"
        query = {**sign_params(), **(params or {})}
        resp = self.session.request(
            method, self._base + path, params=query, headers=headers,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
            timeout=timeout or self.timeout,
        )
        resp.raise_for_status()
        data = _decode_response(resp)

        if "repCode" in data:  # captcha endpoints use a different envelope
            return data

        if data.get("code") != 0:
            code, err = data.get("code"), data.get("errorCode")
            msg = data.get("message") or f"API error code={code} errorCode={err}"
            if authed and (code in _AUTH_CODES or err in _AUTH_ERROR_CODES):
                if retry_auth and path != "/system/login/refreshToken" and self.renew_login():
                    return self._request(method, path, params=params, body=body, authed=authed,
                                         retry_auth=False, api_v2=api_v2, timeout=timeout)
                raise AuthError(f"Login expired: {msg}")
            raise ApiError(msg)
        return data.get("data")

    def refresh_token(self) -> bool:
        """Swap the refresh token for a new token pair. Returns False if impossible."""
        if not self.auth or not self.auth.refresh_token:
            return False
        try:
            data = self._request("POST", "/system/login/refreshToken",
                                 body={"refreshToken": self.auth.refresh_token}, retry_auth=False)
        except (ApiError, requests.RequestException):
            return False
        self._set_auth(authmod.Auth(token=data["token"],
                                    refresh_token=data.get("refreshToken") or self.auth.refresh_token))
        return True

    def renew_login(self) -> bool:
        """Get a working token: refresh token first, then the ``relogin`` hook."""
        if self.refresh_token():
            return True
        if self.relogin:
            new = self.relogin()
            if new:
                self._set_auth(new)
                return True
        return False

    def _set_auth(self, new: authmod.Auth) -> None:
        self.auth = new
        if self.persist_auth:
            authmod.save(new)

    def cas_login_url(self, redirect_url: str) -> str:
        """The site's SSO login URL that sends the browser back to ``redirect_url``."""
        return self._request("GET", "/cas/address", authed=False, api_v2=False,
                             params={"redirectUrl": redirect_url, "queryParam": redirect_url, "typeCode": ""})

    def server_time_offset(self, samples: int = 12) -> float:
        """Estimate (server clock - local clock) in seconds from HTTP ``Date`` headers.

        ``Date`` only has 1-second resolution, so we poll until the second ticks
        over; the tick happens somewhere inside that request's round trip.
        """
        prev_server: float | None = None
        prev_start = prev_end = 0.0
        for _ in range(samples):
            start = time.time()
            response = self.session.head(self._index, timeout=self.timeout)
            end = time.time()
            server = parsedate_to_datetime(response.headers["Date"]).timestamp()
            if prev_server is not None and server != prev_server:
                # The second flipped between requests; assume each was handled
                # at the middle of its round trip.
                previous_midpoint = (prev_start + prev_end) / 2
                current_midpoint = (start + end) / 2
                local_at_flip = (previous_midpoint + current_midpoint) / 2
                return server - local_at_flip
            prev_server, prev_start, prev_end = server, start, end
            time.sleep(0.09)
        # Never saw a flip; fall back to a coarse (±0.5 s) estimate.
        assert prev_server is not None
        return prev_server + 0.5 - (prev_start + prev_end) / 2

    # ------------------------------------------------------------ endpoints
    def login_user(self) -> dict:
        return self._request("GET", "/system/login/getLoginUser", api_v2=False)

    def list_sites(self, venue: Venue, date: str) -> list[dict]:
        body = {
            "sceneUuid": venue.scene_uuid,
            "resvKind": "CURRENT_RESERVE",
            "devKindUuid": venue.dev_kind_uuid,
            "siteType": "DEV",
            "searchValue": "",
            "siteKindId": "",
            "classTypeEnum": venue.class_type_enum,
            "classTypeUuid": venue.class_type_uuid,
            "reserveDate": date,
            "sceneUseType": venue.scene_use_type,
            "pageSize": 999,
            "pageNum": 1,
        }
        # This endpoint is slow (1-2 s normally), so allow more time under load.
        return self._request("POST", "/api/reserve/current/page", body=body, timeout=25) or []

    def workflow_deploy_uuid(self, form_uuid: str) -> str:
        data = self._request("GET", f"/workflow/process/brief/{form_uuid}", api_v2=False)
        return data["deployUuid"]

    def get_captcha(self) -> Captcha:
        data = self._request("POST", "/system/captcha/drag/get", authed=False, api_v2=False, body={
            "captchaType": _CAPTCHA_TYPE,
            "clientUid": self._captcha_client_uid,
            "ts": int(time.time() * 1000),
        })
        if data.get("repCode") != "0000":
            raise ApiError(f"captcha get failed: {data.get('repMsg')}")
        rep = data["repData"]
        return Captcha(rep["token"], rep["secretKey"], rep["originalImageBase64"], rep["jigsawImageBase64"])

    def check_captcha(self, captcha: Captcha, x: int) -> str | None:
        """Submit a human-chosen slider position.

        Returns the ``captchaVerification`` string that ``addReserve`` expects,
        or ``None`` if the position was wrong.
        """
        point = captcha_point_json(x)
        data = self._request("POST", "/system/captcha/drag/check", authed=False, api_v2=False, body={
            "captchaType": _CAPTCHA_TYPE,
            "pointJson": captcha_encrypt(point, captcha.secret_key),
            "token": captcha.token,
        })
        if data.get("repCode") != "0000" or not data.get("repData", {}).get("result"):
            return None
        return captcha_encrypt(f"{captcha.token}---{point}", captcha.secret_key)

    def add_reserve(self, body: dict) -> dict:
        return self._request("POST", "/api/reserve/addReserve", body=body, timeout=20)

    def order_check(self, resv_ids: list[str], user_id: str) -> dict:
        return self._request("POST", "/resv/order/check", body={"resvUuidList": resv_ids, "userId": user_id})

    def get_order(self, resv_uuid: str) -> dict:
        return self._request("GET", "/resv/order", params={"resvUuid": resv_uuid})
