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


class Punch:
    def __init__(self, workspace: str, pool_config: str, meta: Metagraph, key: MaintainerKey,
                 state: State, split: str = "dev", seed: int = 1, per_suite: int = 150):
        self.ws = workspace
        self.omakase_eval_dir = os.path.join(workspace, "omakase-eval")
        self.pool_config = pool_config
        self.meta = meta
        self.key = key
        self.state = state
        self.split, self.seed, self.per_suite = split, seed, per_suite

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

        # -- gate 2: locked files --
        ok, reason = gates.gate_locked_files(sub.repo_dir, UNLOCKED[comp])
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
    def _rerun_router(self, sub: Submission):
        base = bl.load(os.path.join(sub.repo_dir, "runs", "baselines.dev.json"))
        router = routers.load_router(os.path.join(sub.repo_dir, "submission", "manifest.json"),
                                     os.path.join(sub.repo_dir, "submission"))
        from omakase_eval.workers import Pool

        pool = Pool.from_config(self.pool_config)
        tasks = suites.generate_split(self.split, self.seed)
        results = engine.run_split(router, tasks, pool, self.seed, self.split)
        # King-of-the-hill: significance vs the current champion. Cost is only
        # gated router-vs-router (a reigning champion); at genesis the incumbent
        # is the single-worker floor, so accuracy alone crowns the first champion.
        runs_dir = os.path.join(sub.repo_dir, "runs")
        has_champion = os.path.exists(bl.champion_path(runs_dir))
        incumbent = bl.load_incumbent(runs_dir, bl.deserialize_results(base.best_single_results))
        verdict = score.judge(results, incumbent, base.oracle_accuracy, gate_cost=has_champion)
        if verdict.passed:  # new champion — cache its results so the next challenger must beat it
            bl.write_champion(os.path.join(self.ws, "omakase-router", "runs"), results, self.split, self.seed)
        tx = transcripts.build(tasks, results, self.seed,
                               header={"competition": "omakase-router", "split": self.split, "seed": self.seed})
        tx_dir = os.path.join(sub.repo_dir, "runs", "transcripts")
        blob = {
            "competition": "omakase-router",
            "manifest_sha256": routers.sha256_file(os.path.join(sub.repo_dir, "submission", "manifest.json")),
            "split": self.split, "seed": self.seed, "n_tasks": len(tasks),
            "mde": round(stats.minimum_detectable_effect(len(tasks)), 4),
            "verdict": verdict.to_dict(), "transcript_sha256": transcripts.write(tx, tx_dir),
        }
        return blob, verdict.passed, ("champion" if verdict.passed else None)

    def _rerun_harness(self, sub: Submission):
        out = os.path.join(sub.repo_dir, "runs", f"punch-{sub.pr}.json")
        # exit 0 = tier awarded, exit 1 = valid "did not beat main" — both write the
        # blob. Only a missing blob (real crash) is an error; check=True here would
        # crash the whole run loop on the most common outcome (an honest non-winner).
        proc = subprocess.run(
            [sys.executable, "eval_adapter.py", "--pool", self.pool_config,
             "--split", self.split, "--seed", str(self.seed), "--per-suite", str(self.per_suite),
             "--out", out, "--transcripts", os.path.join(sub.repo_dir, "runs", "transcripts")],
            cwd=sub.repo_dir, capture_output=True, text=True)
        if not os.path.exists(out):
            raise RuntimeError(f"harness eval crashed (rc={proc.returncode}): {proc.stderr[-500:]}")
        with open(out) as f:
            blob = json.load(f)
        blob.pop("task_summary", None)
        return blob, bool(blob["passed"]), blob.get("tier")

    def _rebaseline_harness(self, sub: Submission) -> None:
        subprocess.run(
            [sys.executable, "eval_adapter.py", "--pool", self.pool_config, "--split", self.split,
             "--seed", str(self.seed), "--per-suite", str(self.per_suite), "--rebaseline"],
            cwd=sub.repo_dir, check=True, capture_output=True, text=True)

    # -- helpers -------------------------------------------------------------
    def _write_run_blob(self, comp: str, pr: int, blob: dict) -> None:
        """Persist the scored run (with per-task summary) so the API/dashboard list it."""
        runs_dir = os.path.join(self.ws, comp, "runs")
        tx = tx_mod.read(os.path.join(runs_dir, "transcripts"), blob["transcript_sha256"])
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
