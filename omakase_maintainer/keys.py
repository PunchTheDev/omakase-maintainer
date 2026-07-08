"""The maintainer's ed25519 signing key.

The maintainer signs every ledger entry it publishes, so a reader can verify
"this run was scored and recorded by the maintainer, unaltered" without trusting
the transport. Reproducibility (rerun from source) is the *what*; the signature
is the *who and unchanged*. The public key is published; the private key never
leaves the maintainer host (KMS/HSM in production).
"""
from __future__ import annotations

import json
import os

from nacl import signing


class MaintainerKey:
    def __init__(self, sk: signing.SigningKey):
        self._sk = sk
        self.pubkey_hex = sk.verify_key.encode().hex()

    @classmethod
    def load_or_create(cls, path: str) -> "MaintainerKey":
        if os.path.exists(path):
            with open(path) as f:
                return cls(signing.SigningKey(bytes.fromhex(json.load(f)["secret"])))
        sk = signing.SigningKey.generate()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({"secret": sk.encode().hex(), "public": sk.verify_key.encode().hex()}, f)
        os.chmod(path, 0o600)
        return cls(sk)

    def sign_hex(self, message: bytes) -> str:
        return self._sk.sign(message).signature.hex()


def verify(pubkey_hex: str, message: bytes, sig_hex: str) -> bool:
    try:
        signing.VerifyKey(bytes.fromhex(pubkey_hex)).verify(message, bytes.fromhex(sig_hex))
        return True
    except Exception:  # noqa: BLE001
        return False
