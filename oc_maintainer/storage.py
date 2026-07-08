"""Weights storage — content-addressed, pluggable, encrypt-until-merge.

Dev uses a local content-addressed store; production uses Hippius S3
(decentralized, SigV4). Same interface either way. Miners upload weights
*encrypted to the maintainer's key* so a pending submission can't be copied off
the store; the maintainer decrypts only inside the canonical rerun, and
publishes the key on merge (champion weights are public — the open ratchet).
"""
from __future__ import annotations

import hashlib
import os
from typing import Protocol


class Store(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def public_url(self, key: str) -> str: ...


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalCAS:
    """Dev backend: files under a root, keyed by name; sha verified on read."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self.root, key)

    def put(self, key: str, data: bytes) -> str:
        with open(self._path(key), "wb") as f:
            f.write(data)
        return sha256(data)

    def get(self, key: str) -> bytes:
        with open(self._path(key), "rb") as f:
            return f.read()

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path(key))

    def public_url(self, key: str) -> str:
        return f"file://{os.path.abspath(self._path(key))}"


class HippiusS3:
    """Production backend: Hippius S3 (SigV4) via boto3. Reachable today at
    s3.hippius.com; needs an access_key + secret_key from console.hippius.com.

    Credentials come from the environment (never committed):
        HIPPIUS_ACCESS_KEY, HIPPIUS_SECRET_KEY, HIPPIUS_BUCKET
    """

    ENDPOINT = "https://s3.hippius.com"
    REGION = "decentralized"

    def __init__(self, bucket: str | None = None, endpoint: str | None = None):
        import boto3
        from botocore.config import Config

        self.bucket = bucket or os.environ["HIPPIUS_BUCKET"]
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint or self.ENDPOINT,
            aws_access_key_id=os.environ["HIPPIUS_ACCESS_KEY"],
            aws_secret_access_key=os.environ["HIPPIUS_SECRET_KEY"],
            region_name=self.REGION,
            config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
        )

    def put(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return sha256(data)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def public_url(self, key: str) -> str:
        return f"{self.ENDPOINT}/{self.bucket}/{key}"


def load(config: dict) -> Store:
    """Factory: {'backend':'local','root':...} or {'backend':'hippius','bucket':...}."""
    if config.get("backend", "local") == "local":
        return LocalCAS(config["root"])
    return HippiusS3(bucket=config.get("bucket"), endpoint=config.get("endpoint"))
