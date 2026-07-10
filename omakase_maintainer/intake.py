"""Submission intake — where PRs become Submissions Punch can process.

GitHubIntake is the production path (gh CLI: list open PRs, read the payload
block, checkout the head, post the verdict, merge). LocalIntake is the same
shape driven from an in-memory spec, so the whole pipeline is testable without
a live repo. Both yield identical Submission objects.
"""
from __future__ import annotations

import json
import re
import subprocess

from .punch import Submission

_PAYLOAD_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def extract_payload(pr_body: str) -> dict | None:
    """The single fenced JSON block is the only authoritative content in a PR body."""
    m = _PAYLOAD_BLOCK.search(pr_body or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


class LocalIntake:
    """Drive Punch from explicit specs — the deterministic test/simulation path."""

    def __init__(self, specs: list[dict]):
        # spec: {competition, pr, repo_dir, payload}
        self.specs = specs

    def submissions(self):
        for s in self.specs:
            yield Submission(s["competition"], s["pr"], s["repo_dir"], s["payload"])


class GitHubIntake:
    """Production path over the gh CLI. Requires gh auth + a checkout per repo."""

    def __init__(self, repos: dict[str, str]):
        # {competition: "owner/name"} for gh, plus {competition: local_checkout_dir}
        self.repos = repos

    def _gh(self, *args: str) -> str:
        return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout

    def open_prs(self, competition: str):
        raw = self._gh("pr", "list", "--repo", self.repos[competition], "--state", "open",
                       "--json", "number,body,headRefOid")
        for pr in json.loads(raw):
            payload = extract_payload(pr["body"])
            if payload:
                yield pr["number"], pr["headRefOid"], payload

    def checkout(self, competition: str, pr: int, into: str, trusted_repo_dir: str) -> None:
        """Materialize the PR head in an ISOLATED worktree at `into`, leaving the
        maintainer's trusted checkout (`trusted_repo_dir`) untouched.

        The old `gh pr checkout` switched the branch in place, so the PR's files
        (its manifest, its runs/ baselines, its eval_adapter) overwrote the very
        tree Punch reads canonical hashes and baselines from — defeating the
        trust boundary. A detached worktree keeps the two separate: Punch reads
        canon from `trusted_repo_dir`, scores the artifact in `into`.
        """
        subprocess.run(["git", "-C", trusted_repo_dir, "fetch", "origin",
                        f"pull/{pr}/head:refs/heads/pr-{pr}"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", trusted_repo_dir, "worktree", "add", "--force", "--detach",
                        into, f"pr-{pr}"], check=True, capture_output=True, text=True)

    def cleanup(self, pr: int, into: str, trusted_repo_dir: str) -> None:
        subprocess.run(["git", "-C", trusted_repo_dir, "worktree", "remove", "--force", into],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", trusted_repo_dir, "branch", "-D", f"pr-{pr}"],
                       capture_output=True, text=True)

    def comment(self, competition: str, pr: int, body: str) -> None:
        self._gh("pr", "comment", str(pr), "--repo", self.repos[competition], "--body", body)

    def merge(self, competition: str, pr: int) -> None:
        self._gh("pr", "merge", str(pr), "--repo", self.repos[competition], "--squash")
