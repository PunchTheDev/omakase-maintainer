"""Gate seeds — the private half of the public-dev / private-gate split.

A gate split is only un-memorizable while its seed is secret: the generators are
public, so `seed` *is* the answer key. Three rules follow, and this module is
what enforces them:

1. **High entropy.** 128 bits. Artifacts publish `split_fingerprint(split, seed)`
   so a stale baseline is detectable; that digest is only safe to publish because
   the seed cannot be brute-forced back out of it. A committed `"seed": 1` — the
   old default — was both guessable and published.
2. **Never in git.** The store lives outside any working tree (or in an ignored
   path) and is loaded from `$OMAKASE_GATE_SEED_FILE` / `$OMAKASE_GATE_SEED`.
   `rotate()` refuses to write somewhere git is tracking.
3. **Rotated per round, published on retirement.** Yesterday's seed is harmless
   and makes past verdicts auditable: anyone can regenerate a retired split and
   re-run the receipts. `retire()` moves the current seed into public history.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
from dataclasses import dataclass

SEED_BITS = 128
ENV_SEED = "OMAKASE_GATE_SEED"
ENV_FILE = "OMAKASE_GATE_SEED_FILE"


class SeedError(RuntimeError):
    """No usable gate seed, or an attempt to persist one where it would leak."""


def _tracked_by_git(path: str) -> bool:
    """True if git would commit this file. A gate seed in a repo is a published seed."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        inside = subprocess.run(["git", "-C", directory, "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True, timeout=5)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return False  # not a repo at all
        ignored = subprocess.run(["git", "-C", directory, "check-ignore", "-q", os.path.abspath(path)],
                                 capture_output=True, timeout=5)
        return ignored.returncode != 0  # exit 0 = ignored (safe); anything else = git would see it
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass(frozen=True)
class Round:
    number: int
    seed: int


class SeedStore:
    """The current gate seed plus the retired ones (which are public)."""

    def __init__(self, path: str) -> None:
        self.path = path

    # -- reading -------------------------------------------------------------
    def _read(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def current(self) -> Round:
        blob = self._read()
        if "seed" not in blob:
            raise SeedError(
                f"no gate seed in {self.path!r} — run `omakase-maintainer rotate-seed` on the "
                f"trusted host, or set ${ENV_SEED}. A gate round must never fall back to a default."
            )
        return Round(int(blob.get("round", 1)), int(blob["seed"], 16))

    def retired(self) -> list[Round]:
        return [Round(r["round"], int(r["seed"], 16)) for r in self._read().get("retired", [])]

    # -- writing -------------------------------------------------------------
    def rotate(self) -> Round:
        """Retire the current seed (making it public) and mint the next round's."""
        if _tracked_by_git(self.path):
            raise SeedError(
                f"refusing to write a gate seed to {self.path!r}: git is not ignoring it. "
                "Keep the seed store outside the working tree or add it to .gitignore."
            )
        blob = self._read()
        retired = blob.get("retired", [])
        if "seed" in blob:
            retired.append({"round": blob.get("round", 1), "seed": blob["seed"]})
        number = int(blob.get("round", 0)) + 1
        seed = secrets.randbits(SEED_BITS)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"round": number, "seed": f"{seed:032x}", "retired": retired}, f, indent=1)
        return Round(number, seed)


def resolve(split: str, config_seed: int | None, seed_file: str | None,
            public_split: str = "dev") -> int:
    """The single place a run's seed is decided.

    Public split: the committed seed is the point — everyone self-scores on it.
    Private split: the seed may only come from the environment or the secret
    store. A config-file seed is refused rather than silently trusted, because
    the config is committed and a published gate seed is a solved benchmark.
    """
    if split == public_split:
        return int(config_seed if config_seed is not None else 1)

    env = os.environ.get(ENV_SEED)
    if env:
        return int(env, 0)
    path = os.environ.get(ENV_FILE) or seed_file
    if path:
        return SeedStore(path).current().seed
    raise SeedError(
        f"split {split!r} is private: set ${ENV_SEED}, ${ENV_FILE}, or `gate_seed_file` in the "
        "maintainer config. Refusing to score a gate round with a committed seed."
    )
