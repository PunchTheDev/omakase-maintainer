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
    rpc_url: str = "wss://entrypoint-finney.opentensor.ai:443"
    binding_url: str | None = None

    def _subtensor(self):
        from substrateinterface import SubstrateInterface  # lazy

        return SubstrateInterface(url=self.rpc_url)

    def is_registered(self, hotkey: str) -> bool:
        sub = self._subtensor()
        result = sub.query("SubtensorModule", "Uids", [self.netuid, hotkey])
        return result.value is not None

    def github_for(self, hotkey: str) -> str | None:
        if not self.binding_url:
            raise RuntimeError("binding_url (das-gittensor) not configured")
        import urllib.request

        with urllib.request.urlopen(f"{self.binding_url}/binding/{hotkey}", timeout=10) as r:
            return json.load(r).get("github_login")


def load(config: dict) -> Metagraph:
    """Factory: {'backend': 'local'|'substrate', ...}."""
    if config.get("backend", "local") == "local":
        return LocalRegistry.from_file(config["path"])
    return SubstrateMetagraph(
        netuid=config.get("netuid", 74),
        rpc_url=config.get("rpc_url", "wss://entrypoint-finney.opentensor.ai:443"),
        binding_url=config.get("binding_url"),
    )
