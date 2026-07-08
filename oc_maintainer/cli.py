"""oc-maintainer CLI — operate Peggy."""
from __future__ import annotations

import argparse
import json
import os
import time

from . import metagraph
from .intake import GitHubIntake, LocalIntake
from .keys import MaintainerKey
from .peggy import Peggy
from .state import State


def _load(config_path: str) -> dict:
    with open(config_path) as f:
        cfg = json.load(f)
    cfg["_dir"] = os.path.dirname(os.path.abspath(config_path))
    return cfg


def _build_peggy(cfg: dict, now: float) -> Peggy:
    base = cfg["_dir"]
    rel = lambda p: p if os.path.isabs(p) else os.path.join(base, p)  # noqa: E731
    meta = metagraph.load({**cfg["metagraph"], "path": rel(cfg["metagraph"].get("path", ""))})
    key = MaintainerKey.load_or_create(rel(cfg["key_path"]))
    state = State(rel(cfg["state_db"]), now)
    return Peggy(rel(cfg["workspace"]), rel(cfg["pool_config"]), meta, key, state,
                 split=cfg.get("split", "dev"), seed=cfg.get("seed", 1), per_suite=cfg.get("per_suite", 150))


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
    peggy = _build_peggy(cfg, now)
    with open(args.spec) as f:
        specs = json.load(f)["submissions"]
    base = cfg["_dir"]
    for s in specs:
        s["repo_dir"] = s["repo_dir"] if os.path.isabs(s["repo_dir"]) else os.path.join(base, s["repo_dir"])
    for sub in LocalIntake(specs).submissions():
        d = peggy.process(sub)
        print(f"{sub.competition}#{sub.pr}: {d.status.upper()} — {d.reason}"
              + (f" (tier {d.tier})" if d.tier else ""))
    return 0


def cmd_run(args):
    """Production loop: poll GitHub, process each open PR, comment + merge."""
    cfg = _load(args.config)
    peggy = _build_peggy(cfg, time.time())
    gh = GitHubIntake({c: v["repo"] for c, v in cfg["repos"].items() if "repo" in v})
    for comp in cfg["repos"]:
        if "repo" not in cfg["repos"][comp]:
            continue
        for pr, head, payload in gh.open_prs(comp):
            checkout = cfg["repos"][comp]["dir"]
            gh.checkout(comp, pr, checkout)
            from .peggy import Submission

            d = peggy.process(Submission(comp, pr, checkout, payload))
            gh.comment(comp, pr, f"Peggy verdict: **{d.status}** — {d.reason}"
                       + (f" (tier `{d.tier}`)" if d.tier else ""))
            if d.status == "merged":
                gh.merge(comp, pr)
    return 0


def cmd_snapshot(args):
    cfg = _load(args.config)
    peggy = _build_peggy(cfg, time.time())
    peggy.state.publish_snapshots(os.path.join(peggy.ws, "oc-maintainer", "state"))
    print("published queue/metrics/miners snapshots")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="oc-maintainer", description=__doc__)
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

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
