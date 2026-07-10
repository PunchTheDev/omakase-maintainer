"""Punch — the maintainer agent. The outer loop that makes it a competition.

process() takes one submission and drives it end to end: parse → gate walk →
admission control → canonical rerun on the trusted host → verdict → signed
ledger entry + label/merge, or close with a reason. Enforcement of rate-limit /
open-PR / credibility / banlist is real (state.py), not advisory.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass

from omakase_eval import baselines as bl
from omakase_eval import engine, frontier, routers, score, stats, suites, transcripts
from omakase_eval import transcripts as tx_mod

from . import gates, signing
from .keys import MaintainerKey
from .metagraph import Metagraph
from .state import MECH_FAIL_DECAY, RERUN_FAIL_DECAY, State

REQUIRED = {
    "omakase-router": ["competition", "hotkey", "github_login", "weights_sha256", "self_score", "signature"],
    "omakase-harness": ["competition", "hotkey", "github_login", "claimed_delta", "self_score", "signature"],
}
UNLOCKED = {"omakase-router": ("submission/", "runs/"), "omakase-harness": ("harness/", "runs/")}


@dataclass
class Submission:
    competition: str
    pr: int
    repo_dir: str  # working tree at the PR head
    payload: dict


@dataclass
class Decision:
    status: str  # merged | closed
    reason: str
    tier: str | None = None
    blob: dict | None = None
    entry_sha: str | None = None


PUBLIC_SPLIT = "dev"  # the only split whose seed and transcripts may be published


class Punch:
    GATE_SEED_MIN = 1 << 32  # a private-split seed below this is brute-forceable → reject

    def __init__(self, workspace: str, pool_config: str, meta: Metagraph, key: MaintainerKey,
                 state: State, split: str = "dev", seed: int = 1, per_suite: int = 150,
                 private_dir: str | None = None, sandbox_mode: str = "process",
                 allow_unisolated_gate: bool = False, trusted_ws: str | None = None):
        # A real round runs untrusted PR code on the box holding the signing key and
        # the gate seed. The process sandbox blinds that code, but same-uid file
        # reads are still a path to the key — so a private split demands OS-level
        # isolation unless the operator asserts the host itself is disposable
        # (an isolated runner that holds no key: the recommended topology).
        if split != PUBLIC_SPLIT and sandbox_mode == "process" and not allow_unisolated_gate:
            raise ValueError(
                f"split {split!r} scores real stakes: run the harness under `sandbox_mode='docker'`, "
                "or set allow_unisolated_gate=true only on a runner that holds no signing key "
                "and no gate seed on disk."
            )
        # A private-split seed below 2**32 is guessable — split_fingerprint and the
        # generators make it a public answer key. Refuse it (the default seed=1 for
        # the public split is fine; the danger is a gate round left on a small seed).
        if split != PUBLIC_SPLIT and seed < self.GATE_SEED_MIN:
            raise ValueError(
                f"split {split!r} needs a high-entropy seed (got {seed}); use `rotate-seed` / "
                "$OMAKASE_GATE_SEED. A small gate seed is a published answer key."
            )
        self.ws = workspace
        # The maintainer's OWN checkout — the trusted source for the canonical
        # manifest and baselines. In production the PR is checked out into a
        # SEPARATE dir (sub.repo_dir); trusted_ws stays clean. In dev they coincide.
        self.trusted_ws = trusted_ws or workspace
        self.omakase_eval_dir = os.path.join(workspace, "omakase-eval")
        self.pool_config = pool_config
        self.meta = meta
        self.key = key
        self.state = state
        self.split, self.seed, self.per_suite = split, seed, per_suite
        self.sandbox_mode = sandbox_mode
        self.allow_unisolated_gate = allow_unisolated_gate
        # On a private split the transcript is *committed to* (its sha lands in the
        # signed ledger) but not *published* — its bytes hold every gate prompt and
        # answer. It is written here instead, and released when the round retires.
        self.public = split == PUBLIC_SPLIT
        self.private_dir = private_dir or os.path.join(workspace, "omakase-maintainer", "state", "private")

    def _canonical_locked(self, comp: str) -> dict:
        """The trusted {path: sha256} locked map, from the maintainer's own checkout —
        NOT the submission's manifest (which the miner controls)."""
        path = os.path.join(self.trusted_ws, comp, "manifest.json")
        try:
            with open(path) as f:
                return json.load(f).get("locked", {})
        except (FileNotFoundError, json.JSONDecodeError):
            return {}  # gate_locked_files fails closed on an empty map

    def _trusted_runs(self, comp: str) -> str:
        return os.path.join(self.trusted_ws, comp, "runs")

    def process(self, sub: Submission) -> Decision:
        comp = sub.competition
        sub_id = f"{comp}#{sub.pr}"
        p = sub.payload

        # -- parse (schema) --
        missing = [k for k in REQUIRED.get(comp, []) if k not in p]
        if missing:
            return self._close(sub, sub_id, f"payload-malformed:{missing}", mech=True, register=False)

        self.state.touch_miner(p["hotkey"], p["github_login"])

        # -- gate 1: identity --
        ok, reason = gates.gate_identity(p, self.meta)
        if not ok:
            return self._close(sub, sub_id, reason, mech=True)

        # -- admission control (rate / ban / open-PR) --
        ok, reason = self.state.admit(p["hotkey"], comp)
        if not ok:
            return self._close(sub, sub_id, f"rate-limited:{reason}", mech=False, decrement=False)

        artifact_sha = p.get("weights_sha256", p.get("head_sha", ""))
        self.state.enqueue(sub_id, comp, sub.pr, p["hotkey"], p["github_login"], artifact_sha)
        self.state.set_status(sub_id, "running")

        # -- gate 2: locked files (verified against the maintainer's manifest, not the PR's) --
        ok, reason = gates.gate_locked_files(sub.repo_dir, UNLOCKED[comp], self._canonical_locked(comp))
        if not ok:
            return self._close(sub, sub_id, reason, mech=True)

        # -- gate 3: artifact --
        ok, reason = (gates.gate_artifact_router(sub.repo_dir, self.omakase_eval_dir) if comp == "omakase-router"
                      else gates.gate_artifact_harness(sub.repo_dir))
        if not ok:
            return self._close(sub, sub_id, reason, mech=True)

        # -- gate 4: canonical rerun (the expensive, trusted-host step) --
        blob, passed, tier = (self._rerun_router(sub) if comp == "omakase-router" else self._rerun_harness(sub))
        self._write_run_blob(comp, sub.pr, blob)

        if not passed:
            self.state.adjust_credibility(p["hotkey"], -RERUN_FAIL_DECAY)
            self.state.set_status(sub_id, "closed", reason="not-significant", tier=None)
            self._snapshot()
            return Decision("closed", "not-significant", blob=blob)

        # -- reward: signed ledger entry + label + merge --
        if comp == "omakase-harness":  # main advances to the merged harness — the next PR must beat it
            self._rebaseline_harness(sub)
        entry = self._append_signed(comp, {
            "competition": comp, "pr": sub.pr, "hotkey": p["hotkey"], "label": tier,
            "accuracy": blob.get("accuracy") or blob["verdict"]["candidate"]["accuracy"],
            "delta": blob.get("delta"), "manifest_sha256": blob.get("manifest_sha256"),
            "transcript_sha256": blob["transcript_sha256"],
        })
        self.state.set_status(sub_id, "merged", reason="win", tier=tier)
        self._snapshot()
        return Decision("merged", "win", tier=tier, blob=blob, entry_sha=entry["sha"])

    # -- canonical reruns ----------------------------------------------------
    def transcript_dir(self, comp: str) -> str:
        """Public splits publish transcripts in-repo; private splits withhold the bytes."""
        if self.public:
            return os.path.join(self.ws, comp, "runs", "transcripts")
        return os.path.join(self.private_dir, comp, "transcripts")

    def _rerun_router(self, sub: Submission):
        from omakase_eval.workers import Pool

        pool = Pool.from_config(self.pool_config)
        # The baseline + champion cache are MAINTAINER artifacts — read them from
        # the trusted checkout, never from the submission's runs/ (an unlocked
        # prefix the miner controls; a forged all-wrong incumbent would hand out a
        # free crown). Stamps (seed fingerprint + pool + suite version) are now
        # required, so a hand-crafted baseline is refused.
        runs_dir = self._trusted_runs("omakase-router")
        base = bl.load_for(runs_dir, self.split, self.seed, pool)
        router = routers.load_router(os.path.join(sub.repo_dir, "submission", "manifest.json"),
                                     os.path.join(sub.repo_dir, "submission"))

        tasks = suites.generate_split(self.split, self.seed)
        results = engine.run_split(router, tasks, pool, self.seed, self.split)
        # King-of-the-hill: significance vs the current champion. Cost is only
        # gated router-vs-router (a reigning champion); at genesis the incumbent
        # is the single-worker floor, so accuracy alone crowns the first champion.
        has_champion = os.path.exists(bl.champion_path(runs_dir))
        incumbent = bl.load_incumbent(runs_dir, bl.deserialize_results(base.best_single_results),
                                      self.split, self.seed)
        verdict = score.judge(results, incumbent, base.oracle_accuracy, gate_cost=has_champion)
        if verdict.passed:  # new champion — cache its results so the next challenger must beat it
            bl.write_champion(runs_dir, results, self.split, self.seed)

        header = {"competition": "omakase-router", "split": self.split}
        if self.public:  # a private split's seed is the answer key — never publish it
            header["seed"] = self.seed
        tx = transcripts.build(tasks, results, self.seed, header=header)
        blob = {
            "competition": "omakase-router",
            "manifest_sha256": routers.sha256_file(os.path.join(sub.repo_dir, "submission", "manifest.json")),
            "split": self.split, "n_tasks": len(tasks),
            "mde": round(stats.minimum_detectable_effect(len(tasks)), 4),
            "verdict": verdict.to_dict(),
            "transcript_sha256": transcripts.write(tx, self.transcript_dir("omakase-router")),
        }
        if self.public:
            blob["seed"] = self.seed
        return blob, verdict.passed, ("champion" if verdict.passed else None)

    def _adapter_env(self) -> dict:
        """The seed rides in the environment, never argv — `ps` is world-readable."""
        return {**os.environ, "OMAKASE_SEED": str(self.seed)}

    def _trusted_harness(self) -> str:
        return os.path.join(self.trusted_ws, "omakase-harness")

    def _harness_baseline(self) -> str:
        return os.path.join(self._trusted_runs("omakase-harness"), "main-baseline.json")

    def _adapter_gate_flags(self) -> list[str]:
        # Forward the keyless-runner assertion so the trusted adapter's own gate
        # guard agrees with Punch's (else a permitted process-mode gate run is
        # rejected by the adapter).
        return ["--allow-unisolated-gate"] if self.allow_unisolated_gate else []

    def _rerun_harness(self, sub: Submission):
        out = os.path.join(sub.repo_dir, "runs", f"punch-{sub.pr}.json")
        # Run the TRUSTED adapter (from the maintainer's checkout) against the PR's
        # harness/ dir, with the maintainer's own main-baseline. The PR's copy of
        # eval_adapter.py — the grader that holds the seed and does the metering —
        # is never executed; a tampered one can't self-declare passed:true or print
        # the seed. exit 0 = won, exit 1 = honest non-winner; both write the blob.
        proc = subprocess.run(
            [sys.executable, "eval_adapter.py", "--pool", self.pool_config,
             "--split", self.split, "--per-suite", str(self.per_suite),
             "--sandbox", self.sandbox_mode, *self._adapter_gate_flags(),
             "--harness-dir", os.path.join(sub.repo_dir, "harness"),
             "--baseline", self._harness_baseline(),
             "--out", out, "--transcripts", self.transcript_dir("omakase-harness")],
            cwd=self._trusted_harness(), capture_output=True, text=True, env=self._adapter_env())
        if not os.path.exists(out):
            raise RuntimeError(f"harness eval crashed (rc={proc.returncode}): {proc.stderr[-500:]}")
        with open(out) as f:
            blob = json.load(f)
        blob.pop("task_summary", None)
        # Same king-of-the-hill label as Router: a significant win IS the crown.
        return blob, bool(blob["passed"]), ("champion" if blob["passed"] else None)

    def _rebaseline_harness(self, sub: Submission) -> None:
        # Rebaseline main from the PR's now-merged harness, writing the maintainer's
        # trusted baseline (never the PR's runs/).
        subprocess.run(
            [sys.executable, "eval_adapter.py", "--pool", self.pool_config, "--split", self.split,
             "--per-suite", str(self.per_suite), "--sandbox", self.sandbox_mode, "--rebaseline",
             *self._adapter_gate_flags(),
             "--harness-dir", os.path.join(sub.repo_dir, "harness"),
             "--baseline", self._harness_baseline()],
            cwd=self._trusted_harness(), check=True, capture_output=True, text=True, env=self._adapter_env())

    # -- helpers -------------------------------------------------------------
    def _write_run_blob(self, comp: str, pr: int, blob: dict) -> None:
        """Persist the scored run (with per-task summary) so the API/dashboard list it.

        The summary carries per-task ids and correctness — the McNemar evidence —
        but never a prompt or an answer, so it is publishable even for a gate run
        whose full transcript is withheld.
        """
        runs_dir = os.path.join(self.ws, comp, "runs")
        tx = tx_mod.read(self.transcript_dir(comp), blob["transcript_sha256"])
        record = {**blob, "pr": pr, "task_summary": tx_mod.summarize(tx) if tx else []}
        os.makedirs(runs_dir, exist_ok=True)
        with open(os.path.join(runs_dir, f"run-{pr}.json"), "w") as f:
            json.dump(record, f, indent=1)

    def _append_signed(self, comp: str, payload: dict) -> dict:
        runs_dir = os.path.join(self.ws, comp, "runs")
        entry = frontier.append(os.path.join(runs_dir, "frontier.jsonl"), "merge", payload, ts=self.state.now)
        signing.sign_entry(self.key, runs_dir, entry["sha"])
        return entry

    def _close(self, sub: Submission, sub_id: str, reason: str, mech: bool,
               decrement: bool = True, register: bool = True) -> Decision:
        if register and sub.payload.get("hotkey"):
            self.state.touch_miner(sub.payload["hotkey"], sub.payload.get("github_login", ""))
        if decrement and sub.payload.get("hotkey"):
            self.state.adjust_credibility(sub.payload["hotkey"], -(MECH_FAIL_DECAY if mech else RERUN_FAIL_DECAY))
        self.state.enqueue(sub_id, sub.competition, sub.pr, sub.payload.get("hotkey", "?"),
                           sub.payload.get("github_login", "?"), sub.payload.get("weights_sha256", ""))
        self.state.set_status(sub_id, "closed", reason=reason)
        self._snapshot()
        return Decision("closed", reason)

    def _snapshot(self) -> None:
        state_dir = os.path.join(self.ws, "omakase-maintainer", "state")
        self.state.publish_snapshots(state_dir)
        # publish the PUBLIC key so any reader can verify ledger signatures (never the secret)
        with open(os.path.join(state_dir, "maintainer.pub.json"), "w") as f:
            json.dump({"alg": "ed25519", "pubkey": self.key.pubkey_hex}, f, indent=1)
