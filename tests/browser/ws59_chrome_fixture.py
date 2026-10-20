"""WS-59 테스트 보조 — 합성 Chrome Cookies DB(테스트용 키로 암호화).

실제 일상 Chrome·키체인은 건드리지 않는다. 암호화는 공개된 macOS Chrome 형식을 그대로 따른다:
PBKDF2-HMAC-SHA1(password, b"saltysalt", 1003, 16) → AES-128-CBC(IV 공백 16, PKCS7), 접두 b"v10".
meta.version ≥ 24 이면 평문 앞에 SHA256(host_key) 32바이트가 붙는다.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: 테스트용 키체인 비밀(합성 — 실제 키체인 값이 아님).
TEST_PASSWORD = b"ws59-test-safe-storage-pw"
#: 1601-01-01 → 1970-01-01 (초)
EPOCH_DELTA = 11644473600

SCHEMA = """
CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
CREATE TABLE cookies(creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL, top_frame_site_key TEXT NOT NULL,
  name TEXT NOT NULL, value TEXT NOT NULL, encrypted_value BLOB NOT NULL, path TEXT NOT NULL,
  expires_utc INTEGER NOT NULL, is_secure INTEGER NOT NULL, is_httponly INTEGER NOT NULL,
  last_access_utc INTEGER NOT NULL, has_expires INTEGER NOT NULL, is_persistent INTEGER NOT NULL,
  priority INTEGER NOT NULL, samesite INTEGER NOT NULL, source_scheme INTEGER NOT NULL,
  source_port INTEGER NOT NULL, last_update_utc INTEGER NOT NULL, source_type INTEGER NOT NULL,
  has_cross_site_ancestor INTEGER NOT NULL);
"""


def derive_test_key(password: bytes = TEST_PASSWORD) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)


def encrypt(plain: str, host_key: str, *, hash_prefix: bool, password: bytes = TEST_PASSWORD,
            prefix: bytes = b"v10") -> bytes:
    data = plain.encode("utf-8")
    if hash_prefix:
        data = hashlib.sha256(host_key.encode("utf-8")).digest() + data
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(derive_test_key(password)), modes.CBC(b" " * 16)).encryptor()
    return prefix + enc.update(padded) + enc.finalize()


def chrome_time(unix: float) -> int:
    return int((unix + EPOCH_DELTA) * 1_000_000)


def make_db(path: Path, meta_version: Optional[int], rows: Iterable[dict]) -> Path:
    """rows: host, name, value(평문), enc('v10'|'plain'|bytes), path, expires(unix|None), secure, httponly,
    samesite, partition."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    if meta_version is not None:
        db.execute("INSERT INTO meta VALUES('version', ?)", (str(meta_version),))
    now = chrome_time(time.time())
    for r in rows:
        host = r["host"]
        enc = r.get("enc", "v10")
        if enc == "v10":
            value_col, enc_col = "", encrypt(r["value"], host, hash_prefix=(meta_version or 0) >= 24)
        elif enc == "plain":
            value_col, enc_col = r["value"], b""
        else:
            value_col, enc_col = "", enc
        expires = r.get("expires", time.time() + 86400)
        has_exp = 0 if expires is None else 1
        db.execute(
            "INSERT INTO cookies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, host, r.get("partition", ""), r["name"], value_col, enc_col, r.get("path", "/"),
             0 if expires is None else chrome_time(expires), int(r.get("secure", False)),
             int(r.get("httponly", False)), now, has_exp, has_exp, 1, r.get("samesite", -1), 2, 443,
             now, 0, 0),
        )
    db.commit()
    db.close()
    return path
