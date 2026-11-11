"""Request signing and the small bits of crypto the venue site uses.

Ported from the booking site's own frontend JS:

* every API call carries ``appId``, ``timeStamp``, ``nonce`` and ``sign``
  query params, where ``sign = md5("appId=..&nonce=..&timeStamp=..&key=<KEY>")``;
* some responses may arrive as an AES-CBC encrypted string instead of JSON;
* the slider captcha answer is AES-ECB encrypted with a per-challenge key.

The ``appId`` and signing key are not baked in here — they come from the
``[site]`` section of your local config (see :mod:`config`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import config

_NONCE_ALPHABET = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz0123456789"


def app_id() -> str:
    return config.site().app_id


def sign_key() -> str:
    return config.site().sign_key


def make_sign(timestamp_ms: int, nonce: str) -> str:
    raw = f"appId={app_id()}&nonce={nonce}&timeStamp={timestamp_ms}&key={sign_key()}"
    return hashlib.md5(raw.encode()).hexdigest()


def sign_params() -> dict[str, str | int]:
    """Fresh ``appId/timeStamp/nonce/sign`` query params for one request."""
    ts = int(time.time() * 1000)
    nonce = "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(32))
    return {"appId": app_id(), "timeStamp": ts, "nonce": nonce, "sign": make_sign(ts, nonce)}


def decrypt_response(ciphertext_b64: str) -> str:
    """Decrypt a string response body (AES-CBC, zero IV, ISO-10126 padding)."""
    cipher = AES.new(sign_key().encode(), AES.MODE_CBC, iv=b"0" * 16)
    plain = cipher.decrypt(base64.b64decode(ciphertext_b64))
    return plain[: -plain[-1]].decode("utf-8")


def captcha_encrypt(plaintext: str, secret_key: str) -> str:
    """AES-ECB/PKCS7 encrypt, base64 encoded, as the slider widget does."""
    cipher = AES.new(secret_key.encode(), AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(plaintext.encode(), AES.block_size))).decode()


def captcha_point_json(x: int) -> str:
    """The exact point string the widget produces (``JSON.stringify({x, y: 5})``)."""
    return json.dumps({"x": x, "y": 5}, separators=(",", ":"))
