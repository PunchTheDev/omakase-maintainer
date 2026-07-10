"""omakase-maintainer CLI — operate Punch."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import metagraph, seeds
from .intake import GitHubIntake, LocalIntake
from .keys import MaintainerKey
from .punch import Punch
from .state import State


def _load(config_path: str) -> dict:
    with open(config_path) as f:
        cfg = json.load(f)
    cfg["_dir"] = os.path.dirname(os.path.abspath(config_path))
    return cfg


def _build_punch(cfg: dict, now: float) -> Punch:
    base = os.path.dirname(cfg["_dir"])  # config lives in configs/; paths are repo-root relative
    rel = lambda p: p if os.path.isabs(p) else os.path.join(base, p)  # noqa: E731
    meta = metagraph.load({**cfg["metagraph"], "path": rel(cfg["metagraph"].get("path", ""))})
    key = MaintainerKey.load_or_create(rel(cfg["key_path"]))
    state = State(rel(cfg["state_db"]), now)
    split = cfg.get("split", "dev")
    seed_file = cfg.get("gate_seed_file")
    # On a private split this refuses the committed config seed outright.
    seed = seeds.resolve(split, cfg.get("seed"), rel(seed_file) if seed_file else None)
    trusted = cfg.get("trusted_workspace")
    return Punch(rel(cfg["workspace"]), rel(cfg["pool_config"]), meta, key, state,
                 split=split, seed=seed, per_suite=cfg.get("per_suite", 150),
                 sandbox_mode=cfg.get("sandbox", "process"),
                 allow_unisolated_gate=cfg.get("allow_unisolated_gate", False),
                 trusted_ws=rel(trusted) if trusted else None)


def cmd_keygen(args):
    key = MaintainerKey.load_or_create(args.path)
    print(f"maintainer pubkey: {key.pubkey_hex}")
    return 0


def cmd_register(args):
    path = args.registry
    reg = json.load(open(path)) if os.path.exists(path) else {"miners": {}}
    reg["miners"][args.hotkey] = {"github_login": args.github, "registered_ts": 0}
    with open(path, "w") as f:
        json.dump(reg, f, indent=1)
    print(f"registered {args.hotkey[:12]}… ↔ {args.github}")
    return 0


def cmd_process_local(args):
    cfg = _load(args.config)
    now = args.now if args.now else time.time()
    punch = _build_punch(cfg, now)
    with open(args.spec) as f:
        specs = json.load(f)["submissions"]
    base = cfg["_dir"]
    for s in specs:
        s["repo_dir"] = s["repo_dir"] if os.path.isabs(s["repo_dir"]) else os.path.join(base, s["repo_dir"])
    for sub in LocalIntake(specs).submissions():
        d = punch.process(sub)
        print(f"{sub.competition}#{sub.pr}: {d.status.upper()} — {d.reason}"
              + (f" ({d.tier})" if d.tier else ""))
    return 0


def cmd_run(args):
    """Production loop: poll GitHub, process each open PR in isolation, comment + merge.

    Each PR is materialized in a throwaway worktree — never over the maintainer's
    trusted checkout, which is what Punch reads canonical manifests and baselines
    from. Untrusted PR files (their manifest, runs/ baselines, eval_adapter) stay
    quarantined in the worktree.
    """
    import shutil
    import tempfile

    from .punch import Submission

    cfg = _load(args.config)
    base = os.path.dirname(cfg["_dir"])
    rel = lambda p: p if os.path.isabs(p) else os.path.join(base, p)  # noqa: E731
    punch = _build_punch(cfg, time.time())
    gh = GitHubIntake({c: v["repo"] for c, v in cfg["repos"].items() if "repo" in v})
    for comp in cfg["repos"]:
        if "repo" not in cfg["repos"][comp]:
            continue
        trusted_repo = rel(cfg["repos"][comp]["dir"])  # the clean checkout Punch trusts
        for pr, head, payload in gh.open_prs(comp):
            work = tempfile.mkdtemp(prefix=f"omakase-pr-{comp}-{pr}-")
            try:
                gh.checkout(comp, pr, work, trusted_repo)
                d = punch.process(Submission(comp, pr, work, payload))
            finally:
                gh.cleanup(pr, work, trusted_repo)
                shutil.rmtree(work, ignore_errors=True)
            gh.comment(comp, pr, f"Punch verdict: **{d.status}** — {d.reason}"
                       + (f" (`{d.tier}`)" if d.tier else ""))
            if d.status == "merged":
                gh.merge(comp, pr)
    return 0


def cmd_snapshot(args):
    cfg = _load(args.config)
    punch = _build_punch(cfg, time.time())
    punch.state.publish_snapshots(os.path.join(punch.ws, "omakase-maintainer", "state"))
    print("published queue/metrics/miners snapshots")
    return 0


def cmd_bump_pin(args):
    """Weekly reset: pin the current Router champion into Harness, re-baseline main.

    Run on a schedule (see scripts/weekly_reset.sh). Reads the reigning router
    champion, copies its weights into omakase-harness/pinned, updates
    router-pin.json, re-baselines the harness against the new pin, and records a
    signed 'reset' entry. Idempotent when the champion is unchanged.
    """
    import hashlib
    import shutil

    cfg = _load(args.config)
    punch = _build_punch(cfg, time.time())
    ws = punch.ws
    src = os.path.join(ws, "omakase-router", "submission", "weights.json")
    dst = os.path.join(ws, "omakase-harness", "pinned", "router-weights.json")
    with open(src, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()

    pin_path = os.path.join(ws, "omakase-harness", "router-pin.json")
    current = json.load(open(pin_path)) if os.path.exists(pin_path) else {}
    if current.get("weights_sha256") == sha:
        print(f"pin already current ({sha[:12]}…) — no bump")
        return 0

    shutil.copy(src, dst)
    json.dump({"source": "omakase-router current champion", "weights_sha256": sha, "arch": "tiny-linear"},
              open(pin_path, "w"), indent=1)
    import subprocess

    harness_dir = os.path.join(ws, "omakase-harness")
    subprocess.run([sys.executable, "scripts/gen_manifest.py"], cwd=harness_dir, check=True, capture_output=True)
    # re-baseline the harness main against the new pinned router
    punch._rebaseline_harness(type("S", (), {"repo_dir": os.path.join(ws, "omakase-harness")})())
    entry = punch._append_signed("omakase-harness",
                                 {"competition": "omakase-harness", "note": f"weekly pin bump → champion {sha[:12]}"})
    punch._snapshot()
    print(f"pinned champion {sha[:12]}… into Harness; re-baselined; signed reset {entry['sha'][:12]}")
    return 0


def cmd_rotate_seed(args):
    """Retire the current gate seed (it becomes public) and mint the next round's."""
    store = seeds.SeedStore(args.path)
    rnd = store.rotate()
    print(f"round {rnd.number}: new gate seed minted ({seeds.SEED_BITS}-bit), stored in {args.path}")
    print(f"{len(store.retired())} retired seed(s) now publishable — past receipts are reproducible")
    print("the seed itself is never printed — export OMAKASE_GATE_SEED_FILE to use it")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="omakase-maintainer", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("keygen"); s.add_argument("--path", default="state/maintainer.key"); s.set_defaults(fn=cmd_keygen)
    s = sub.add_parser("register")
    s.add_argument("hotkey"); s.add_argument("github"); s.add_argument("--registry", default="configs/registry.dev.json")
    s.set_defaults(fn=cmd_register)
    s = sub.add_parser("process-local")
    s.add_argument("--config", default="configs/maintainer.dev.json"); s.add_argument("--spec", required=True)
    s.add_argument("--now", type=float, default=0.0); s.set_defaults(fn=cmd_process_local)
    s = sub.add_parser("run"); s.add_argument("--config", default="configs/maintainer.dev.json"); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("snapshot"); s.add_argument("--config", default="configs/maintainer.dev.json"); s.set_defaults(fn=cmd_snapshot)
    s = sub.add_parser("bump-pin", help="weekly: pin the Router champion into Harness + re-baseline")
    s.add_argument("--config", default="configs/maintainer.dev.json"); s.set_defaults(fn=cmd_bump_pin)
    s = sub.add_parser("rotate-seed", help="retire the current gate seed and mint the next round's")
    s.add_argument("--path", default="state/gate-seed.json", help="secret seed store; must be git-ignored")
    s.set_defaults(fn=cmd_rotate_seed)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
