"""Full outer loop: a real signed miner PR → Peggy → gates → rerun → signed merge.

Exercises identity (sr25519), the gate walk, king-of-the-hill scoring, the
signed ledger entry, and the enforced limits — against a copy of the real
oc-router working tree.
"""
import json
import os
import shutil
import subprocess

import pytest
from substrateinterface import Keypair

from oc_eval import frontier
from oc_maintainer import identity, signing
from oc_maintainer.keys import MaintainerKey
from oc_maintainer.metagraph import LocalRegistry
from oc_maintainer.peggy import Peggy, Submission
from oc_maintainer.state import State

REPO = os.path.join(os.path.dirname(__file__), "..", "..", "oc-router")
POOL = os.path.join(os.path.dirname(__file__), "..", "..", "oc-eval", "configs", "pool.dev.json")


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    dst = ws / "oc-router"
    shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
    # gate_locked_files enumerates the git tree — make the copy a real repo
    subprocess.run(["git", "init", "-q"], cwd=dst, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dst, check=True)
    champ = dst / "runs" / "champion-baseline.json"
    if champ.exists():
        champ.unlink()  # genesis: no incumbent, the best-single floor applies
    return ws


def _signed_payload(kp, github, weights_sha):
    p = {"competition": "oc-router", "hotkey": kp.ss58_address, "github_login": github,
         "weights_sha256": weights_sha, "self_score": {"accuracy": 0.9, "split": "dev", "seed": 1}}
    p["signature"] = identity.sign_payload(kp, p)
    return p


def _peggy(ws, registry, now):
    key = MaintainerKey.load_or_create(str(ws / "mk.key"))
    state = State(str(ws / "state.db"), now)
    return Peggy(str(ws), os.path.abspath(POOL), registry, key, state, split="dev", seed=1)


@pytest.fixture
def genesis(tmp_path, pool_server):
    ws = _workspace(tmp_path)
    kp = Keypair.create_from_uri("//MinerGenesis")
    weights_sha = json.load(open(ws / "oc-router" / "submission" / "manifest.json"))["weights_sha256"]
    registry = LocalRegistry({kp.ss58_address: {"github_login": "genesis-miner"}})
    peggy = _peggy(ws, registry, now=1_000_000.0)
    payload = _signed_payload(kp, "genesis-miner", weights_sha)
    decision = peggy.process(Submission("oc-router", 1, str(ws / "oc-router"), payload))
    return ws, peggy, registry, decision


def test_genesis_submission_merges_and_is_signed(genesis):
    ws, peggy, _, d = genesis
    assert d.status == "merged" and d.tier == "champion", d.reason

    entries = frontier.read(str(ws / "oc-router" / "runs" / "frontier.jsonl"))
    merge = entries[-1]
    assert merge["kind"] == "merge" and merge["payload"]["label"] == "champion"
    assert "transcript_sha256" in merge["payload"]

    # the ledger entry is signed by the maintainer
    assert signing.verify_entry(str(ws / "oc-router" / "runs"), merge["sha"], peggy.key.pubkey_hex)
    # a champion baseline now exists for the next challenger to beat
    assert (ws / "oc-router" / "runs" / "champion-baseline.json").exists()


def test_king_of_the_hill_blocks_equal_router(genesis):
    ws, peggy, registry, _ = genesis
    # a second miner resubmits the identical champion — cannot beat the incumbent
    kp2 = Keypair.create_from_uri("//MinerCopycat")
    registry.entries[kp2.ss58_address] = {"github_login": "copycat"}
    weights_sha = json.load(open(ws / "oc-router" / "submission" / "manifest.json"))["weights_sha256"]
    peggy.state.now = 2_000_000.0
    payload = _signed_payload(kp2, "copycat", weights_sha)
    d = peggy.process(Submission("oc-router", 2, str(ws / "oc-router"), payload))
    assert d.status == "closed" and d.reason == "not-significant"


def test_unregistered_hotkey_rejected_before_compute(genesis):
    ws, peggy, _, _ = genesis
    stranger = Keypair.create_from_uri("//Stranger")
    weights_sha = json.load(open(ws / "oc-router" / "submission" / "manifest.json"))["weights_sha256"]
    payload = _signed_payload(stranger, "stranger", weights_sha)
    d = peggy.process(Submission("oc-router", 3, str(ws / "oc-router"), payload))
    assert d.status == "closed" and d.reason == "identity-unregistered"


def test_bad_signature_rejected(genesis):
    ws, peggy, registry, _ = genesis
    kp = Keypair.create_from_uri("//MinerBadSig")
    registry.entries[kp.ss58_address] = {"github_login": "badsig"}
    weights_sha = json.load(open(ws / "oc-router" / "submission" / "manifest.json"))["weights_sha256"]
    payload = _signed_payload(kp, "badsig", weights_sha)
    payload["signature"] = "0x" + "00" * 64  # forged
    d = peggy.process(Submission("oc-router", 4, str(ws / "oc-router"), payload))
    assert d.status == "closed" and d.reason == "identity-bad-signature"
