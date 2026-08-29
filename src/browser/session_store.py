"""세션 스토리지 암호화 (PRD §5.1-1).

Playwright `storageState` JSON을 AES-256-GCM으로 암호화해
`~/.agent-browser/auth/{profile_name}.enc`에 파일 권한 0600으로 저장한다.

규격:
* 암호화: AES-256-GCM (인증 태그 128-bit)
* KDF: Argon2id (salt 16B, iterations=3, memory_cost=65536 KiB, lanes=4)
* Nonce: 암호화마다 96-bit CSPRNG 신규 생성 (재사용 원천 차단)
* 키 우선순위: OS Keyring → CLI 패스프레이즈 → CI 환경변수(경고 로그)

파일 포맷 (헤더로 Nonce/Salt를 자기 기술):
    magic(4) | version(1) | salt(16) | nonce(12) | ciphertext+tag
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# --- 포맷 상수 -------------------------------------------------------------
MAGIC = b"ABAU"  # Agent-Browser AUth
FORMAT_VERSION = 1
SALT_BYTES = 16
NONCE_BYTES = 12  # 96-bit
KEY_BYTES = 32  # AES-256
HEADER_LEN = len(MAGIC) + 1 + SALT_BYTES + NONCE_BYTES

# --- Argon2id 파라미터 (PRD §5.1-1) ----------------------------------------
ARGON2_ITERATIONS = 3
ARGON2_MEMORY_COST_KIB = 65536
ARGON2_LANES = 4

# --- 키 공급원 -------------------------------------------------------------
KEYRING_SERVICE = "agent-browser"
CI_ENV_VAR = "AGENT_AUTH_KEY_CI"

DEFAULT_AUTH_DIR = Path.home() / ".agent-browser" / "auth"

#: 파일 권한 0600 (소유자 읽기/쓰기)
FILE_MODE = 0o600
DIR_MODE = 0o700


class SessionStoreError(RuntimeError):
    """세션 스토어 처리 실패."""


class KeyUnavailableError(SessionStoreError):
    """어떤 우선순위에서도 마스터 키를 얻지 못함."""


class DecryptionError(SessionStoreError):
    """복호화 실패 (키 불일치 또는 변조)."""


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Argon2id로 패스프레이즈에서 AES-256 키를 유도한다."""
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

    kdf = Argon2id(
        salt=salt,
        length=KEY_BYTES,
        iterations=ARGON2_ITERATIONS,
        lanes=ARGON2_LANES,
        memory_cost=ARGON2_MEMORY_COST_KIB,
    )
    return kdf.derive(passphrase.encode("utf-8"))


@dataclass
class KeyResolution:
    """마스터 키 해석 결과 (어느 공급원에서 왔는지 추적)."""

    passphrase: str
    source: str  # "keyring" | "prompt" | "ci_env"


def resolve_passphrase(
    profile_name: str,
    *,
    prompt_fn=None,
    allow_prompt: bool = True,
) -> KeyResolution:
    """PRD §5.1-1 키 우선순위에 따라 마스터 패스프레이즈를 얻는다.

    1) OS Keyring  2) CLI 프롬프트  3) CI 환경변수(경고 로그)
    """
    # 1순위: OS Keyring
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, profile_name)
        if stored:
            return KeyResolution(passphrase=stored, source="keyring")
    except Exception as exc:  # noqa: BLE001 - keyring 백엔드 부재 등
        logger.debug("Keyring 조회 실패: %s", exc)

    # 2순위: CLI 마스터 패스프레이즈 프롬프트
    if allow_prompt and prompt_fn is not None:
        entered = prompt_fn(f"[{profile_name}] 마스터 패스프레이즈: ")
        if entered:
            return KeyResolution(passphrase=entered, source="prompt")

    # 3순위: CI 환경변수 (보안 등급이 낮으므로 경고)
    ci_value = os.environ.get(CI_ENV_VAR)
    if ci_value:
        logger.warning(
            "마스터 키를 환경변수 %s에서 읽었습니다. CI 환경 외 사용을 권장하지 않습니다.",
            CI_ENV_VAR,
        )
        return KeyResolution(passphrase=ci_value, source="ci_env")

    raise KeyUnavailableError(
        f"프로파일 '{profile_name}'의 마스터 키를 얻지 못했습니다 "
        f"(keyring / 프롬프트 / {CI_ENV_VAR} 모두 실패)."
    )


class SessionStore:
    """암호화된 `storageState` 저장소."""

    def __init__(self, auth_dir: Optional[Path] = None) -> None:
        self.auth_dir = Path(auth_dir) if auth_dir else DEFAULT_AUTH_DIR

    # -- 경로 ---------------------------------------------------------------

    def path_for(self, profile_name: str) -> Path:
        if not profile_name or "/" in profile_name or "\\" in profile_name:
            raise SessionStoreError(f"잘못된 프로파일 이름: {profile_name!r}")
        return self.auth_dir / f"{profile_name}.enc"

    def _ensure_dir(self) -> None:
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.auth_dir, DIR_MODE)

    # -- 암복호화 -----------------------------------------------------------

    def encrypt(self, storage_state: Dict[str, Any], passphrase: str) -> bytes:
        """storageState를 암호화해 자기 기술 헤더가 붙은 바이트로 반환한다."""
        salt = secrets.token_bytes(SALT_BYTES)
        nonce = secrets.token_bytes(NONCE_BYTES)  # 매 암호화마다 신규 생성
        key = _derive_key(passphrase, salt)

        plaintext = json.dumps(storage_state, ensure_ascii=False).encode("utf-8")
        # 헤더를 AAD로 묶어 버전/솔트/논스 변조를 인증 태그로 탐지
        header = MAGIC + bytes([FORMAT_VERSION]) + salt + nonce
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, header)
        return header + ciphertext

    def decrypt(self, blob: bytes, passphrase: str) -> Dict[str, Any]:
        """암호문을 복호화해 storageState를 반환한다."""
        if len(blob) < HEADER_LEN or not blob.startswith(MAGIC):
            raise DecryptionError("알 수 없는 파일 포맷입니다.")

        version = blob[len(MAGIC)]
        if version != FORMAT_VERSION:
            raise DecryptionError(f"지원하지 않는 포맷 버전: {version}")

        offset = len(MAGIC) + 1
        salt = blob[offset : offset + SALT_BYTES]
        nonce = blob[offset + SALT_BYTES : HEADER_LEN]
        ciphertext = blob[HEADER_LEN:]
        header = blob[:HEADER_LEN]

        key = _derive_key(passphrase, salt)
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, header)
        except InvalidTag as exc:
            raise DecryptionError(
                "복호화에 실패했습니다 (패스프레이즈 불일치 또는 파일 변조)."
            ) from exc
        return json.loads(plaintext.decode("utf-8"))

    # -- 파일 I/O -----------------------------------------------------------

    def save(
        self, profile_name: str, storage_state: Dict[str, Any], passphrase: str
    ) -> Path:
        """암호화해 0600 권한으로 저장한다."""
        self._ensure_dir()
        path = self.path_for(profile_name)
        blob = self.encrypt(storage_state, passphrase)

        # 원자적 교체 + 권한을 쓰기 전에 설정해 평문 노출 창을 없앤다.
        tmp = path.with_suffix(".enc.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, path)
        if os.name != "nt":
            os.chmod(path, FILE_MODE)
        return path

    def load(self, profile_name: str, passphrase: str) -> Dict[str, Any]:
        path = self.path_for(profile_name)
        if not path.exists():
            raise SessionStoreError(f"세션 파일이 없습니다: {path}")
        return self.decrypt(path.read_bytes(), passphrase)

    def exists(self, profile_name: str) -> bool:
        return self.path_for(profile_name).exists()

    def delete(self, profile_name: str) -> bool:
        path = self.path_for(profile_name)
        if path.exists():
            path.unlink()
            return True
        return False

    def verify_permissions(self, profile_name: str) -> bool:
        """저장 파일이 0600인지 확인한다 (Windows는 검사 생략)."""
        if os.name == "nt":
            return True
        path = self.path_for(profile_name)
        if not path.exists():
            return False
        return stat.S_IMODE(path.stat().st_mode) == FILE_MODE
