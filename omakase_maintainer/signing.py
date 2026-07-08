"""Signatures over ledger entries, stored beside the frontier log.

The maintainer signs each entry's chain hash (`sha`, which commits to the whole
entry). Signatures live in runs/signatures.json — a side map {entry_sha:
record} — so the frontier format stays untouched and any reader (the dashboard,
a miner) can verify each entry against the published maintainer pubkey.
"""
from __future__ import annotations

import json
import os

from .keys import MaintainerKey, verify


def _path(runs_dir: str) -> str:
    return os.path.join(runs_dir, "signatures.json")


def load(runs_dir: str) -> dict:
    try:
        with open(_path(runs_dir)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def sign_entry(key: MaintainerKey, runs_dir: str, entry_sha: str) -> dict:
    store = load(runs_dir)
    record = {"alg": "ed25519", "by": "maintainer", "pubkey": key.pubkey_hex,
              "sig": key.sign_hex(entry_sha.encode())}
    store[entry_sha] = record
    os.makedirs(runs_dir, exist_ok=True)
    with open(_path(runs_dir), "w") as f:
        json.dump(store, f, indent=1, sort_keys=True)
    return record


def verify_entry(runs_dir: str, entry_sha: str, pubkey_hex: str) -> bool:
    rec = load(runs_dir).get(entry_sha)
    return bool(rec) and rec["pubkey"] == pubkey_hex and verify(pubkey_hex, entry_sha.encode(), rec["sig"])
