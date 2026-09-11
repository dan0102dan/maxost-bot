from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Vault:
    """Versioned AEAD envelope. Owner and purpose are authenticated, not just encrypted.

    First key is used for writes; all listed keys can decrypt existing records.
    Keep the oldest key last while rotating: it anchors phone rate-limit fingerprints.
    """

    def __init__(self, encoded_keys: str):
        raw = [base64.urlsafe_b64decode(k.strip()) for k in encoded_keys.split(",")]
        if not raw or any(len(k) != 32 for k in raw):
            raise ValueError("ENCRYPTION_KEYS must contain base64-encoded 32-byte keys")
        self.keys = {hashlib.sha256(k).digest()[:8]: AESGCM(k) for k in raw}
        self.active = next(iter(self.keys))
        self.fingerprint_key = raw[-1]

    @staticmethod
    def context(owner: int, purpose: str) -> bytes:
        return f"maxost:v1:{owner}:{purpose}".encode()

    def seal(self, owner: int, purpose: str, value: Any) -> bytes:
        nonce = os.urandom(12)
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        return b"M1" + self.active + nonce + self.keys[self.active].encrypt(
            nonce, payload, self.context(owner, purpose)
        )

    def open(self, owner: int, purpose: str, blob: bytes) -> Any:
        if len(blob) < 38 or blob[:2] != b"M1" or blob[2:10] not in self.keys:
            raise ValueError("Unknown or corrupt encrypted envelope")
        plain = self.keys[blob[2:10]].decrypt(blob[10:22], blob[22:], self.context(owner, purpose))
        return json.loads(plain)

    def digest(self, value: str) -> str:
        return hmac.new(self.fingerprint_key, value.encode(), hashlib.sha256).hexdigest()
