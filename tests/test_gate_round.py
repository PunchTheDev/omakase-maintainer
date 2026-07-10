"""A real (private) gate round: secret seed, sealed transcripts, isolated exec.

The dev split is public by design — seed 1, transcripts published, everything
reproducible. A gate round inverts all of that, and these tests pin the
inversion: a committed seed is refused, a published seed is never written, the
champion cache cannot be paired across a rotation, and untrusted code is not
allowed to run beside the signing key.
"""
import json
import os
import shutil
import subprocess

import pytest
from substrateinterface import Keypair

from omakase_eval import baselines as bl
from omakase_eval import suites
from omakase_eval.workers import Pool
from omakase_maintainer import identity, seeds
from omakase_maintainer.keys import MaintainerKey
from omakase_maintainer.metagraph import LocalRegistry
from omakase_maintainer.punch import Punch, Submission
from omakase_maintainer.state import State

REPO = os.path.join(os.path.dirname(__file__), "..", "..", "omakase-router")
POOL = os.path.join(os.path.dirname(__file__), "..", "..", "omakase-eval", "configs", "pool.dev.json")

GATE_SEED = 0x9E3779B97F4A7C15F39CC0605CEDC835  # a realistic 128-bit round seed


# -- seed store (#11) ---------------------------------------------------------

def test_gate_split_refuses_a_committed_config_seed():
    """The old default — `"seed": 1` in a committed config — is a published answer key."""
    with pytest.raises(seeds.SeedError, match="private"):
        seeds.resolve("gate", config_seed=1, seed_file=None)


def test_public_split_still_uses_the_committed_seed():
    assert seeds.resolve("dev", config_seed=1, seed_file=None) == 1


def test_gate_seed_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(seeds.ENV_SEED, hex(GATE_SEED))
    assert seeds.resolve("gate", config_seed=1, seed_file=None) == GATE_SEED


def test_rotation_mints_high_entropy_seeds_and_retires_the_old_one(tmp_path):
    store = seeds.SeedStore(str(tmp_path / "gate-seed.json"))
    first = store.rotate()
    second = store.rotate()
    assert first.seed != second.seed
    assert first.seed.bit_length() > 100, "a low-entropy seed is brute-forceable from its fingerprint"
    assert store.current().seed == second.seed
    assert [r.seed for r in store.retired()] == [first.seed], "retired seeds must become auditable"
    assert oct(os.stat(store.path).st_mode)[-3:] == "600"


def test_rotation_refuses_a_git_tracked_path(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    store = seeds.SeedStore(str(tmp_path / "gate-seed.json"))  # not ignored → git would commit it
    with pytest.raises(seeds.SeedError, match="ignoring"):
        store.rotate()


def test_rotation_allows_a_git_ignored_path(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("gate-seed.json\n")
    seeds.SeedStore(str(tmp_path / "gate-seed.json")).rotate()  # must not raise


# -- isolation guard (#12) ----------------------------------------------------

def _punch(ws, registry, split="dev", seed=1, **kw):
    key = MaintainerKey.load_or_create(str(ws / "mk.key"))
    state = State(str(ws / "state.db"), 1_000_000.0)
    return Punch(str(ws), os.path.abspath(POOL), registry, key, state, split=split, seed=seed, **kw)


def test_gate_round_refuses_to_run_untrusted_code_beside_the_key(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ValueError, match="isolat|docker"):
        _punch(ws, LocalRegistry({}), split="gate", seed=GATE_SEED, sandbox_mode="process")


def test_gate_round_accepts_an_explicitly_disposable_runner(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    p = _punch(ws, LocalRegistry({}), split="gate", seed=GATE_SEED,
               sandbox_mode="process", allow_unisolated_gate=True)
    assert not p.public


# -- sealed publication (#11) -------------------------------------------------

def test_gate_transcripts_are_written_outside_the_public_repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    gate = _punch(ws, LocalRegistry({}), split="gate", seed=GATE_SEED, sandbox_mode="docker")
    dev = _punch(ws, LocalRegistry({}), split="dev", seed=1)
    assert "omakase-router/runs/transcripts" in dev.transcript_dir("omakase-router").replace(os.sep, "/")
    gate_dir = gate.transcript_dir("omakase-router").replace(os.sep, "/")
    assert "runs/transcripts" not in gate_dir, "gate prompts+answers would be published"
    assert "state/private" in gate_dir


# -- baseline stamping (#13) --------------------------------------------------

def test_a_dev_baseline_cannot_judge_a_gate_run(tmp_path):
    """The exact bug: gate tasks scored against baselines.dev.json — disjoint ids."""
    runs = tmp_path
    pool = Pool.from_config(os.path.abspath(POOL))
    blob = {
        "split": "dev", "seed": 1, "solo": {}, "solo_axes": {}, "best_single": "w",
        "best_single_results": [], "oracle_accuracy": 0.9,
        "best_worker_per_task": {}, "seed_fingerprint": suites.split_fingerprint("dev", 1),
        "pool_version": pool.version, "suite_version": suites.SUITE_VERSION,
    }
    (runs / "baselines.dev.json").write_text(json.dumps(blob))

    # asking for the gate baseline finds nothing — it must never silently fall back to dev
    with pytest.raises(bl.StaleBaseline, match="no baseline for split 'gate'"):
        bl.load_for(str(runs), "gate", GATE_SEED, pool)

    # and a gate baseline stamped with the wrong seed is refused too
    gate_blob = {**blob, "split": "gate", "seed": None,
                 "seed_fingerprint": suites.split_fingerprint("gate", 111)}
    (runs / "baselines.gate.json").write_text(json.dumps(gate_blob))
    with pytest.raises(bl.StaleBaseline, match="seed fingerprint"):
        bl.load_for(str(runs), "gate", GATE_SEED, pool)

    # a forged baseline that simply omits the stamps must ALSO be refused (the old
    # `if base.seed_fingerprint and ...` let an empty string skip the check)
    forged = {**blob, "split": "gate", "seed_fingerprint": "", "pool_version": "", "suite_version": ""}
    (runs / "baselines.gate.json").write_text(json.dumps(forged))
    with pytest.raises(bl.StaleBaseline):
        bl.load_for(str(runs), "gate", GATE_SEED, pool)


def test_baseline_refuses_a_changed_pool(tmp_path):
    pool = Pool.from_config(os.path.abspath(POOL))
    blob = {
        "split": "dev", "seed": 1, "solo": {}, "solo_axes": {}, "best_single": "w",
        "best_single_results": [], "oracle_accuracy": 0.9, "best_worker_per_task": {},
        "seed_fingerprint": suites.split_fingerprint("dev", 1),
        "pool_version": "pool@stale", "suite_version": suites.SUITE_VERSION,
    }
    (tmp_path / "baselines.dev.json").write_text(json.dumps(blob))
    with pytest.raises(bl.StaleBaseline, match="pool"):
        bl.load_for(str(tmp_path), "dev", 1, pool)


def test_baseline_refuses_changed_generators(tmp_path):
    pool = Pool.from_config(os.path.abspath(POOL))
    blob = {
        "split": "dev", "seed": 1, "solo": {}, "solo_axes": {}, "best_single": "w",
        "best_single_results": [], "oracle_accuracy": 0.9, "best_worker_per_task": {},
        "seed_fingerprint": suites.split_fingerprint("dev", 1),
        "pool_version": pool.version, "suite_version": "suites@v0",
    }
    (tmp_path / "baselines.dev.json").write_text(json.dumps(blob))
    with pytest.raises(bl.StaleBaseline, match="generators"):
        bl.load_for(str(tmp_path), "dev", 1, pool)


def test_gate_baseline_never_stores_its_seed(tmp_path, pool_server):
    """A committed baseline for a live round must not carry the key to that round."""
    pool = Pool.from_config(os.path.abspath(POOL))
    base = bl.compute(pool, "gate", GATE_SEED)
    assert base.seed is None
    assert base.seed_fingerprint == suites.split_fingerprint("gate", GATE_SEED)
    assert GATE_SEED not in json.loads(base.to_json()).values()


# -- attacker-supplied-data trust boundaries (the two criticals) --------------

def _checkout(dst):
    shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
    subprocess.run(["git", "init", "-q"], cwd=dst, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dst, check=True)
    champ = os.path.join(dst, "runs", "champion-baseline.json")
    if os.path.exists(champ):
        os.remove(champ)


def _seed_registry(hotkey, github):
    return LocalRegistry({hotkey: {"github_login": github}})


def test_tampered_locked_file_is_caught_by_the_canonical_manifest(tmp_path):
    """The eval_adapter-swap exploit: a miner edits a locked file, updates their OWN
    manifest hash to match, and the old gate passed. Now the gate authenticates
    against the maintainer's manifest, so a self-consistent tampered PR is rejected."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _checkout(str(ws / "omakase-router"))            # trusted maintainer checkout
    pr = tmp_path / "pr" / "omakase-router"
    _checkout(str(pr))                                # attacker's PR checkout

    # tamper a locked file in the PR and rewrite the PR's OWN manifest hash to match
    manifest = json.load(open(pr / "manifest.json"))
    victim = next(iter(manifest["locked"]))          # any locked file — e.g. a doc or code file
    (pr / victim).write_text((pr / victim).read_text() + "\n# injected\n")
    import hashlib
    manifest["locked"][victim] = hashlib.sha256((pr / victim).read_bytes()).hexdigest()
    json.dump(manifest, open(pr / "manifest.json", "w"))
    subprocess.run(["git", "add", "-A"], cwd=pr, check=True)

    kp = Keypair.create_from_uri("//Tamper")
    punch = _punch(ws, _seed_registry(kp.ss58_address, "tamper"))  # trusted_ws defaults to ws
    sha = json.load(open(pr / "submission" / "manifest.json"))["weights_sha256"]
    payload = {"competition": "omakase-router", "hotkey": kp.ss58_address, "github_login": "tamper",
               "weights_sha256": sha, "self_score": {"accuracy": 0.9, "split": "dev", "seed": 1}}
    payload["signature"] = identity.sign_payload(kp, payload)
    d = punch.process(Submission("omakase-router", 1, str(pr), payload))
    assert d.status == "closed" and d.reason.startswith("locked-file-modified"), d.reason


def test_forged_incumbent_baseline_in_the_pr_is_ignored(tmp_path, pool_server):
    """The router baseline + champion cache are read from the maintainer's checkout,
    not the PR's runs/. A forged all-wrong incumbent shipped in the PR grants nothing."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _checkout(str(ws / "omakase-router"))
    pr = tmp_path / "pr" / "omakase-router"
    _checkout(str(pr))

    # forge a baseline in the PR: every incumbent answer wrong → any router "wins"
    real = json.load(open(pr / "runs" / "baselines.dev.json"))
    forged = {**real, "best_single_results": [{**r, "correct": False} for r in real["best_single_results"]]}
    json.dump(forged, open(pr / "runs" / "baselines.dev.json", "w"))
    subprocess.run(["git", "add", "-A"], cwd=pr, check=True)

    kp = Keypair.create_from_uri("//ForgeBaseline")
    punch = _punch(ws, _seed_registry(kp.ss58_address, "forge"))
    sha = json.load(open(pr / "submission" / "manifest.json"))["weights_sha256"]
    payload = {"competition": "omakase-router", "hotkey": kp.ss58_address, "github_login": "forge",
               "weights_sha256": sha, "self_score": {"accuracy": 0.9, "split": "dev", "seed": 1}}
    payload["signature"] = identity.sign_payload(kp, payload)
    d = punch.process(Submission("omakase-router", 1, str(pr), payload))
    # genesis: the trusted best-single floor is used, not the PR's all-wrong forgery.
    # The champion router legitimately clears the floor, but the forged baseline gives
    # it no artificial edge — the decisive point is the verdict used the TRUSTED base,
    # which we assert by the champion accuracy matching a real rerun, not ~1.0-vs-0.
    assert d.blob is not None
    base = d.blob["verdict"]["baseline"]["accuracy"]
    assert base > 0.4, f"a forged all-wrong incumbent leaked into scoring (baseline acc {base})"
