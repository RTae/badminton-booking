import base64
import hashlib
import json

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

import config
import crypto

# With no [site] in the config, crypto uses the generic placeholder values.
APP_ID = config.PLACEHOLDER_APP_ID
KEY = config.PLACEHOLDER_KEY


def test_sign_is_md5_of_the_canonical_query():
    ts, nonce = 1793430445114, "7FmkGnR8AMTjQEMKRSaFwpHAWcdJXH3Y"
    expected = hashlib.md5(f"appId={APP_ID}&nonce={nonce}&timeStamp={ts}&key={KEY}".encode()).hexdigest()
    assert crypto.make_sign(ts, nonce) == expected


def test_sign_params_shape():
    p = crypto.sign_params()
    assert p["appId"] == APP_ID
    assert len(p["nonce"]) == 32
    assert p["sign"] == crypto.make_sign(p["timeStamp"], p["nonce"])


def test_decrypt_response_roundtrip():
    plain = json.dumps({"code": 0, "data": {"x": "A01"}}, ensure_ascii=False).encode()
    padlen = 16 - len(plain) % 16
    padded = plain + b"\x07" * (padlen - 1) + bytes([padlen])  # ISO-10126: random bytes + length
    ct = AES.new(KEY.encode(), AES.MODE_CBC, iv=b"0" * 16).encrypt(padded)
    assert json.loads(crypto.decrypt_response(base64.b64encode(ct).decode())) == {"code": 0, "data": {"x": "A01"}}


def test_captcha_encrypt_is_aes_ecb_pkcs7():
    key = "oxbMsrYmAB7jtm5w"
    point = crypto.captcha_point_json(123)
    assert point == '{"x":123,"y":5}'
    ct = base64.b64decode(crypto.captcha_encrypt("tok---" + point, key))
    assert unpad(AES.new(key.encode(), AES.MODE_ECB).decrypt(ct), 16).decode() == 'tok---{"x":123,"y":5}'
