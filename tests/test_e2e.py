"""Full outer loop: a real signed miner PR → Punch → gates → rerun → signed merge.

Exercises identity (sr25519), the gate walk, king-of-the-hill scoring, the
signed ledger entry, and the enforced limits — against a copy of the real
omakase-router working tree.
"""
import json
import os
import shutil
import subprocess

import pytest
from substrateinterface import Keypair

from omakase_eval import frontier
from omakase_maintainer import identity, signing
from omakase_maintainer.keys import MaintainerKey
from omakase_maintainer.metagraph import LocalRegistry
from omakase_maintainer.punch import Punch, Submission
from omakase_maintainer.state import State

REPO = os.path.join(os.path.dirname(__file__), "..", "..", "omakase-router")
POOL = os.path.join(os.path.dirname(__file__), "..", "..", "omakase-eval", "configs", "pool.dev.json")


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    dst = ws / "omakase-router"
    shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
    # gate_locked_files enumerates the git tree — make the copy a real repo
    subprocess.run(["git", "init", "-q"], cwd=dst, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dst, check=True)
    champ = dst / "runs" / "champion-baseline.json"
    if champ.exists():
        champ.unlink()  # genesis: no incumbent, the best-single floor applies
    return ws


def _signed_payload(kp, github, weights_sha):
    p = {"competition": "omakase-router", "hotkey": kp.ss58_address, "github_login": github,
         "weights_sha256": weights_sha, "self_score": {"accuracy": 0.9, "split": "dev", "seed": 1}}
    p["signature"] = identity.sign_payload(kp, p)
    return p


def _punch(ws, registry, now):
    key = MaintainerKey.load_or_create(str(ws / "mk.key"))
    state = State(str(ws / "state.db"), now)
    return Punch(str(ws), os.path.abspath(POOL), registry, key, state, split="dev", seed=1)


@pytest.fixture
def genesis(tmp_path, pool_server):
    ws = _workspace(tmp_path)
    kp = Keypair.create_from_uri("//MinerGenesis")
    weights_sha = json.load(open(ws / "omakase-router" / "submission" / "manifest.json"))["weights_sha256"]
    registry = LocalRegistry({kp.ss58_address: {"github_login": "genesis-miner"}})
    punch = _punch(ws, registry, now=1_000_000.0)
    payload = _signed_payload(kp, "genesis-miner", weights_sha)
    decision = punch.process(Submission("omakase-router", 1, str(ws / "omakase-router"), payload))
    return ws, punch, registry, decision


def test_genesis_submission_merges_and_is_signed(genesis):
    ws, punch, _, d = genesis
    assert d.status == "merged" and d.tier == "champion", d.reason

    entries = frontier.read(str(ws / "omakase-router" / "runs" / "frontier.jsonl"))
    merge = entries[-1]
    assert merge["kind"] == "merge" and merge["payload"]["label"] == "champion"
    assert "transcript_sha256" in merge["payload"]

    # the ledger entry is signed by the maintainer
    assert signing.verify_entry(str(ws / "omakase-router" / "runs"), merge["sha"], punch.key.pubkey_hex)
    # a champion baseline now exists for the next challenger to beat
    assert (ws / "omakase-router" / "runs" / "champion-baseline.json").exists()


def test_king_of_the_hill_blocks_equal_router(genesis):
    ws, punch, registry, _ = genesis
    # a second miner resubmits the identical champion — cannot beat the incumbent
    kp2 = Keypair.create_from_uri("//MinerCopycat")
    registry.entries[kp2.ss58_address] = {"github_login": "copycat"}
    weights_sha = json.load(open(ws / "omakase-router" / "submission" / "manifest.json"))["weights_sha256"]
    punch.state.now = 2_000_000.0
    payload = _signed_payload(kp2, "copycat", weights_sha)
    d = punch.process(Submission("omakase-router", 2, str(ws / "omakase-router"), payload))
    assert d.status == "closed" and d.reason == "not-significant"


def test_unregistered_hotkey_rejected_before_compute(genesis):
    ws, punch, _, _ = genesis
    stranger = Keypair.create_from_uri("//Stranger")
    weights_sha = json.load(open(ws / "omakase-router" / "submission" / "manifest.json"))["weights_sha256"]
    payload = _signed_payload(stranger, "stranger", weights_sha)
    d = punch.process(Submission("omakase-router", 3, str(ws / "omakase-router"), payload))
    assert d.status == "closed" and d.reason == "identity-unregistered"


def test_bad_signature_rejected(genesis):
    ws, punch, registry, _ = genesis
    kp = Keypair.create_from_uri("//MinerBadSig")
    registry.entries[kp.ss58_address] = {"github_login": "badsig"}
    weights_sha = json.load(open(ws / "omakase-router" / "submission" / "manifest.json"))["weights_sha256"]
    payload = _signed_payload(kp, "badsig", weights_sha)
    payload["signature"] = "0x" + "00" * 64  # forged
    d = punch.process(Submission("omakase-router", 4, str(ws / "omakase-router"), payload))
    assert d.status == "closed" and d.reason == "identity-bad-signature"
