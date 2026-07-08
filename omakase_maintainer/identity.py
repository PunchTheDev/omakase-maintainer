"""Miner identity + real signature verification (Gate 1).

A submission is signed by the miner's Bittensor hotkey over a deterministic
message derived from the payload. We verify with the ss58 address alone (no
private key) — the same position the maintainer is in. sr25519 (Bittensor's
default) and ed25519 are both accepted.
"""
from __future__ import annotations

import hashlib

from substrateinterface import Keypair
from substrateinterface.utils.ss58 import is_valid_ss58_address

# crypto_type constants from substrate-interface
ED25519, SR25519 = 0, 1


def signing_message(payload: dict) -> bytes:
    """The bytes a miner signs. Deterministic and binding: competition ‖ hotkey ‖ artifact.

    Router binds the weights sha; Harness binds the PR head sha. Binding the hotkey
    stops a leaked artifact from being re-sealed under someone else's identity.
    """
    competition = payload["competition"]
    hotkey = payload["hotkey"]
    artifact = payload.get("weights_sha256") or payload["head_sha"]
    return hashlib.sha256(f"{competition}|{hotkey}|{artifact}".encode()).digest()


def verify_signature(payload: dict) -> bool:
    """True iff `payload['signature']` is a valid hotkey signature over signing_message."""
    hotkey = payload.get("hotkey", "")
    sig = payload.get("signature", "")
    if not is_valid_ss58_address(hotkey) or not isinstance(sig, str):
        return False
    try:
        signature = bytes.fromhex(sig[2:] if sig.startswith("0x") else sig)
    except ValueError:
        return False
    msg = signing_message(payload)
    for crypto_type in (SR25519, ED25519):
        try:
            if Keypair(ss58_address=hotkey, crypto_type=crypto_type).verify(msg, signature):
                return True
        except Exception:  # noqa: BLE001 — wrong curve raises; try the other
            continue
    return False


def sign_payload(keypair: Keypair, payload: dict) -> str:
    """Miner-side helper (and test helper): produce the hex signature for a payload."""
    return "0x" + keypair.sign(signing_message(payload)).hex()
