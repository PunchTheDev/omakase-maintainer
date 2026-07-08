# Hippius weights storage

`omakase_maintainer/storage.py` ships a real Hippius S3 client (SigV4, boto3). It was
verified to reach the live gateway — a probe upload returned
`SignatureDoesNotMatch`, which means the endpoint, client, and signing flow all
work and only the credentials are missing.

## What's needed to activate it

The client authenticates with **S3 access_key + secret_key generated at
[console.hippius.com](https://console.hippius.com)** — these are *not* the same
as the `api.hippius.com` API token (that token returns 401 on the S3 gateway).

Set, never commit:

```bash
export HIPPIUS_ACCESS_KEY=...    # from console.hippius.com
export HIPPIUS_SECRET_KEY=...
export HIPPIUS_BUCKET=oc-weights
```

Then flip the store config from `{"backend":"local"}` to
`{"backend":"hippius","bucket":"oc-weights"}` — no code changes.

## The flow (encrypt-until-merge)

1. Miner seals weights to the maintainer's Curve25519 pubkey
   (`weights_crypto.seal`) and uploads ciphertext to Hippius. The store never
   holds plaintext.
2. The manifest carries `hippius_object_key` + `weights_sha256` + the sealed key.
3. Punch downloads, verifies the sha, unseals inside the canonical rerun
   (`weights_crypto.unseal`) — the only place plaintext exists.
4. On merge, the symmetric key is published (`weights_crypto.reveal_key`):
   champion weights become public — the open ratchet.

Dev uses `LocalCAS` (content-addressed filesystem) behind the identical
interface, so the whole flow is testable without any credentials.
