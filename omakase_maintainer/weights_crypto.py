"""Encrypt-until-merge for submitted weights.

The miner encrypts weights with a random symmetric key (SecretBox) and seals
that key to the maintainer's Curve25519 public key (SealedBox). The store only
ever holds ciphertext; only the maintainer can unseal, and only inside the
canonical rerun. On merge the symmetric key is published, making champion
weights public — the open ratchet. This closes the submit→merge copy window
without round-based commit-reveal.
"""
from __future__ import annotations

import json
import os

from nacl import public, secret, utils


class MaintainerBoxKey:
    """The maintainer's Curve25519 keypair for unsealing submissions."""

    def __init__(self, sk: public.PrivateKey):
        self._sk = sk
        self.pubkey_hex = bytes(sk.public_key).hex()

    @classmethod
    def load_or_create(cls, path: str) -> "MaintainerBoxKey":
        if os.path.exists(path):
            with open(path) as f:
                return cls(public.PrivateKey(bytes.fromhex(json.load(f)["secret"])))
        sk = public.PrivateKey.generate()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({"secret": bytes(sk).hex(), "public": bytes(sk.public_key).hex()}, f)
        os.chmod(path, 0o600)
        return cls(sk)


def seal(data: bytes, maintainer_pubkey_hex: str) -> dict:
    """Miner side: encrypt weights; return the ciphertext package."""
    sym = utils.random(secret.SecretBox.KEY_SIZE)
    ciphertext = secret.SecretBox(sym).encrypt(data)
    sealed_key = public.SealedBox(public.PublicKey(bytes.fromhex(maintainer_pubkey_hex))).encrypt(sym)
    return {"ciphertext": ciphertext.hex(), "sealed_key": sealed_key.hex()}


def unseal(package: dict, box_key: MaintainerBoxKey) -> bytes:
    """Maintainer side: recover the plaintext weights inside the rerun."""
    sym = public.SealedBox(box_key._sk).decrypt(bytes.fromhex(package["sealed_key"]))
    return secret.SecretBox(sym).decrypt(bytes.fromhex(package["ciphertext"]))


def reveal_key(package: dict, box_key: MaintainerBoxKey) -> str:
    """On merge: publish the symmetric key so anyone can decrypt the champion."""
    return public.SealedBox(box_key._sk).decrypt(bytes.fromhex(package["sealed_key"])).hex()
