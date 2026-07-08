"""The gate walk — the mechanical checks Punch runs before the expensive rerun.

Default verdict is reject: a gate passes only on affirmative evidence. Each
returns (ok, reason_code). The order is cheapest-first so spam dies before any
compute is spent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

from . import identity
from .metagraph import Metagraph

# --- Gate 1: identity ------------------------------------------------------


def gate_identity(payload: dict, meta: Metagraph) -> tuple[bool, str]:
    hotkey = payload.get("hotkey", "")
    if not meta.is_registered(hotkey):
        return False, "identity-unregistered"
    bound = meta.github_for(hotkey)
    if bound is None or bound.lower() != payload.get("github_login", "").lower():
        return False, "identity-unbound"
    if not identity.verify_signature(payload):
        return False, "identity-bad-signature"
    return True, "ok"


# --- Gate 2: locked files (tree-authoritative) -----------------------------


def gate_locked_files(repo_dir: str, unlocked_prefixes: tuple[str, ...]) -> tuple[bool, str]:
    with open(os.path.join(repo_dir, "manifest.json")) as f:
        locked = json.load(f)["locked"]
    tracked = subprocess.run(["git", "-C", repo_dir, "ls-files"], capture_output=True, text=True).stdout.split()
    for path in tracked:
        if path == "manifest.json" or path.startswith(unlocked_prefixes):
            continue
        full = os.path.join(repo_dir, path)
        if path not in locked:
            return False, f"locked-file-unlisted:{path}"
        if not os.path.exists(full):
            return False, f"locked-file-deleted:{path}"
        with open(full, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != locked[path]:
                return False, f"locked-file-modified:{path}"
    return True, "ok"


# --- Gate 3: artifact static checks ----------------------------------------

_HARNESS_BANNED = re.compile(
    r"\b(socket|subprocess|urllib|requests|httpx|http\.client"
    r"|suites|mockpool|task_by_id|generate_split|knows"
    r"|importlib|__import__|eval|exec|open|globals|vars)\b")


def gate_artifact_router(repo_dir: str, omakase_eval_dir: str) -> tuple[bool, str]:
    sys.path.insert(0, omakase_eval_dir)
    from omakase_eval import routers  # noqa: E402

    sub = os.path.join(repo_dir, "submission")
    try:
        router = routers.load_router(os.path.join(sub, "manifest.json"), sub)
    except (ValueError, FileNotFoundError, KeyError) as e:
        return False, f"artifact-invalid:{e}"
    if routers.perplexity_check(router) < 0.5:
        return False, "artifact-degenerate"
    return True, "ok"


def gate_artifact_harness(repo_dir: str) -> tuple[bool, str]:
    for dirpath, _, files in os.walk(os.path.join(repo_dir, "harness")):
        for name in files:
            if name.endswith(".py"):
                with open(os.path.join(dirpath, name)) as f:
                    if _HARNESS_BANNED.search(f.read()):
                        return False, f"artifact-banned-primitive:{name}"
    return True, "ok"
