"""Registration + GitHub binding lookup — pluggable so dev needs no chain.

Gate 1 asks two questions: is this hotkey registered on SN74, and is the PR's
GitHub account the one bound to it? The dev backend answers from a local
registry file; the production backend answers from the live metagraph + the
das-gittensor binding. Same interface, swapped by config.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol


class Metagraph(Protocol):
    def is_registered(self, hotkey: str) -> bool: ...
    def github_for(self, hotkey: str) -> str | None: ...


@dataclass
class LocalRegistry:
    """Dev backend: {hotkey: {github_login, registered_ts}} from a JSON file."""

    entries: dict[str, dict]

    @classmethod
    def from_file(cls, path: str) -> "LocalRegistry":
        with open(path) as f:
            return cls(json.load(f)["miners"])

    def is_registered(self, hotkey: str) -> bool:
        return hotkey in self.entries

    def github_for(self, hotkey: str) -> str | None:
        return self.entries.get(hotkey, {}).get("github_login")


@dataclass
class SubstrateMetagraph:
    """Production backend: live SN74 metagraph + das-gittensor GitHub binding.

    Kept import-light and lazy so dev never needs an RPC endpoint. Wire the
    binding source (das-gittensor endpoint) via `binding_url` when going live.
    """

    netuid: int = 74
    rpc_url: str = "wss://rpc.blockmachine.io"  # free public Bittensor RPC, no key
    binding_url: str | None = None

    def _subtensor(self):
        from substrateinterface import SubstrateInterface  # lazy

        return SubstrateInterface(url=self.rpc_url)

    def is_registered(self, hotkey: str) -> bool:
        from substrateinterface.utils.ss58 import is_valid_ss58_address

        if not is_valid_ss58_address(hotkey):
            return False
        result = self._subtensor().query("SubtensorModule", "Uids", [self.netuid, hotkey])
        return result.value is not None

    def github_for(self, hotkey: str) -> str | None:
        if not self.binding_url:
            raise RuntimeError("binding_url (das-gittensor) not configured")
        import urllib.request

        with urllib.request.urlopen(f"{self.binding_url}/binding/{hotkey}", timeout=10) as r:
            return json.load(r).get("github_login")


@dataclass
class GittensorApiMetagraph:
    """Production binding source: the gittensor.io validator API.

    GET /miners returns the validator's registered miners with their
    hotkey → githubUsername/githubId pairing and eligibility. The validator
    enforces **zero changes** to a hotkey↔github pairing, so this is the trust
    anchor for identity — no PAT storage, no on-chain-history problem. We bind
    on the immutable `githubId` (usernames can be renamed on GitHub); the PR's
    author proves GitHub control, the hotkey signature proves hotkey control.
    No auth required. Cached briefly to avoid hammering the endpoint.
    """

    api_url: str = "https://api.gittensor.io"
    ttl_s: float = 60.0
    _cache: dict | None = None
    _fetched_at: float = 0.0

    def _miners(self) -> dict[str, dict]:
        import time
        import urllib.request

        if self._cache is not None and time.monotonic() - self._fetched_at < self.ttl_s:
            return self._cache
        req = urllib.request.Request(f"{self.api_url}/miners", headers={"User-Agent": "omakase-maintainer/0.1"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.load(r)
        self._cache = {row["hotkey"]: row for row in rows}
        self._fetched_at = time.monotonic()
        return self._cache

    def is_registered(self, hotkey: str) -> bool:
        row = self._miners().get(hotkey)
        return bool(row and row.get("isEligible", True))  # eligible = active + scoring

    def github_for(self, hotkey: str) -> str | None:
        return (self._miners().get(hotkey) or {}).get("githubUsername")

    def github_id_for(self, hotkey: str) -> str | None:
        """The immutable numeric GitHub id — the identity to bind on."""
        return (self._miners().get(hotkey) or {}).get("githubId")


def load(config: dict) -> Metagraph:
    """Factory: backend ∈ {'local','substrate','gittensor-api'}."""
    backend = config.get("backend", "local")
    if backend == "local":
        return LocalRegistry.from_file(config["path"])
    if backend == "gittensor-api":
        return GittensorApiMetagraph(api_url=config.get("api_url", "https://api.gittensor.io"))
    return SubstrateMetagraph(
        netuid=config.get("netuid", 74),
        rpc_url=config.get("rpc_url", "wss://rpc.blockmachine.io"),
        binding_url=config.get("binding_url"),
    )
