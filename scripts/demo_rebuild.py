#!/usr/bin/env python3
"""Rebuild a clean, real canonical ledger by driving Punch over a realistic set
of submissions — a genesis champion, a copycat the king-of-the-hill gate rejects,
and an unregistered spammer the pre-gate closes for free. Produces the signed
ledger, champion baseline, maintainer state, and dashboard snapshots.

Run with the mock pool up:  omakase-eval mockpool --port 8100 &
"""
from __future__ import annotations

import json
import os
import sys

from substrateinterface import Keypair

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, os.path.join(ROOT, "omakase-eval"))

from omakase_maintainer import identity, metagraph  # noqa: E402
from omakase_maintainer.keys import MaintainerKey  # noqa: E402
from omakase_maintainer.punch import Punch, Submission  # noqa: E402
from omakase_maintainer.state import State  # noqa: E402

OC_ROUTER = os.path.join(ROOT, "omakase-router")
OC_HARNESS = os.path.join(ROOT, "omakase-harness")
POOL = os.path.join(ROOT, "omakase-eval", "configs", "pool.dev.json")
REGISTRY = os.path.join(HERE, "..", "configs", "registry.dev.json")
STATE_DIR = os.path.join(HERE, "..", "state")

# The zero-improvement seed harness (v2 contract) — main starts here so the
# merged hedge-retry harness is a genuine, measured improvement over it.
SEED_HARNESS = '''"""Seed reference harness (v2): one worker call, no follow-up. The baseline main starts at."""
from __future__ import annotations
from omakase_eval import templates
from omakase_eval.actions import Call


def run_task(router, view, pool, budget) -> str:
    action = router.decide(task=view, prompt=view.prompt, steps=[])
    if not isinstance(action, Call):
        return ""
    return pool.chat(action.worker, templates.SYSTEM["worker"],
                     templates.user_message("worker", view.prompt, None)).text
'''


def signed(kp, github, weights_sha, competition="omakase-router"):
    p = {"competition": competition, "hotkey": kp.ss58_address, "github_login": github,
         "weights_sha256": weights_sha, "self_score": {"accuracy": 0.9, "split": "dev", "seed": 1}}
    p["signature"] = identity.sign_payload(kp, p)
    return p


def signed_harness(kp, github, head_sha):
    p = {"competition": "omakase-harness", "hotkey": kp.ss58_address, "github_login": github,
         "claimed_delta": 0.03, "head_sha": head_sha, "self_score": {"accuracy": 0.93, "split": "dev", "seed": 1}}
    p["signature"] = identity.sign_payload(kp, p)
    return p


def rebuild_harness(punch, miner):
    """Set main to the seed harness, then merge the current (hedge-retry) harness through Punch."""
    import glob
    import hashlib
    import shutil
    import subprocess
    # sync the Harness pin to the current Router champion
    src = os.path.join(OC_ROUTER, "submission", "weights.json")
    shutil.copy(src, os.path.join(OC_HARNESS, "pinned", "router-weights.json"))
    sha = hashlib.sha256(open(src, "rb").read()).hexdigest()
    json.dump({"source": "omakase-router current champion", "weights_sha256": sha, "arch": "tiny-linear"},
              open(os.path.join(OC_HARNESS, "router-pin.json"), "w"), indent=1)
    subprocess.run([sys.executable, "scripts/gen_manifest.py"], cwd=OC_HARNESS, check=True, capture_output=True)
    for f in ("frontier.jsonl", "signatures.json", "hedge-retry.json"):
        path = os.path.join(OC_HARNESS, "runs", f)
        if os.path.exists(path):
            os.remove(path)
    for stale in glob.glob(os.path.join(OC_HARNESS, "runs", "punch-*.json")) + glob.glob(os.path.join(OC_HARNESS, "runs", "run-*.json")):
        os.remove(stale)
    live = os.path.join(OC_HARNESS, "harness", "system.py")
    kept = open(live).read()
    try:
        open(live, "w").write(SEED_HARNESS)
        subprocess.run([sys.executable, "eval_adapter.py", "--pool", POOL, "--split", "dev",
                        "--seed", "1", "--per-suite", "150", "--rebaseline"],
                       cwd=OC_HARNESS, check=True, capture_output=True, text=True)
    finally:
        open(live, "w").write(kept)  # restore the real (improved) harness
    # genesis rebaseline marker in the ledger
    from omakase_eval import frontier
    from omakase_maintainer import signing
    e = frontier.append(os.path.join(OC_HARNESS, "runs", "frontier.jsonl"), "rebaseline",
                        {"competition": "omakase-harness", "note": "genesis: main = seed reference harness"},
                        ts=punch.state.now)
    signing.sign_entry(punch.key, os.path.join(OC_HARNESS, "runs"), e["sha"])
    d = punch.process(Submission("omakase-harness", 1, OC_HARNESS, signed_harness(miner, "harness-miner", "deadbeef")))
    print(f"omakase-harness#1 [harness-miner]: {d.status.upper()} — {d.reason}" + (f" ({d.tier})" if d.tier else ""))


def main() -> int:
    # fresh ledger — clear prior ledger + stale standalone run blobs
    import glob
    for f in ("frontier.jsonl", "signatures.json", "champion-baseline.json", "seed-champion.json"):
        path = os.path.join(OC_ROUTER, "runs", f)
        if os.path.exists(path):
            os.remove(path)
    for stale in glob.glob(os.path.join(OC_ROUTER, "runs", "run-*.json")):
        os.remove(stale)

    weights_sha = json.load(open(os.path.join(OC_ROUTER, "submission", "manifest.json")))["weights_sha256"]

    genesis = Keypair.create_from_uri("//DemoGenesis")
    copycat = Keypair.create_from_uri("//DemoCopycat")
    stranger = Keypair.create_from_uri("//DemoStranger")
    harness_miner = Keypair.create_from_uri("//DemoHarness")

    reg = {"miners": {
        genesis.ss58_address: {"github_login": "genesis-miner", "registered_ts": 0},
        copycat.ss58_address: {"github_login": "copycat-miner", "registered_ts": 0},
        harness_miner.ss58_address: {"github_login": "harness-miner", "registered_ts": 0},
    }}
    json.dump(reg, open(REGISTRY, "w"), indent=1)

    meta = metagraph.LocalRegistry(reg["miners"])
    key = MaintainerKey.load_or_create(os.path.join(STATE_DIR, "maintainer.key"))
    db = os.path.join(STATE_DIR, "maintainer.db")
    if os.path.exists(db):
        os.remove(db)
    state = State(db, now=1_783_500_000.0)
    punch = Punch(ROOT, POOL, meta, key, state, split="dev", seed=1)

    subs = [
        ("genesis-miner", genesis, weights_sha, 1),
        ("copycat-miner", copycat, weights_sha, 2),
        ("stranger", stranger, weights_sha, 3),
    ]
    for github, kp, sha, pr in subs:
        d = punch.process(Submission("omakase-router", pr, OC_ROUTER, signed(kp, github, sha)))
        print(f"omakase-router#{pr} [{github}]: {d.status.upper()} — {d.reason}" + (f" ({d.tier})" if d.tier else ""))

    rebuild_harness(punch, harness_miner)

    punch.state.publish_snapshots(STATE_DIR)
    print(f"maintainer pubkey: {key.pubkey_hex}")
    print("snapshots + signed ledger written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
