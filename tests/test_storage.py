import pytest

from omakase_maintainer import storage, weights_crypto


def test_local_cas_roundtrip_and_sha(tmp_path):
    store = storage.LocalCAS(str(tmp_path / "cas"))
    data = b"router-weights-bytes"
    sha = store.put("w-1.json", data)
    assert sha == storage.sha256(data)
    assert store.exists("w-1.json")
    assert store.get("w-1.json") == data
    assert not store.exists("missing")


def test_encrypt_until_merge_roundtrip(tmp_path):
    box = weights_crypto.MaintainerBoxKey.load_or_create(str(tmp_path / "box.key"))
    weights = b'{"arch":"tiny-linear","weights":[1,2,3]}'

    pkg = weights_crypto.seal(weights, box.pubkey_hex)          # miner encrypts
    assert weights.hex() not in pkg["ciphertext"]               # not plaintext
    assert weights_crypto.unseal(pkg, box) == weights           # maintainer recovers
    # published key lets anyone decrypt post-merge
    from nacl import secret
    key = bytes.fromhex(weights_crypto.reveal_key(pkg, box))
    assert secret.SecretBox(key).decrypt(bytes.fromhex(pkg["ciphertext"])) == weights


def test_wrong_key_cannot_unseal(tmp_path):
    box = weights_crypto.MaintainerBoxKey.load_or_create(str(tmp_path / "a.key"))
    other = weights_crypto.MaintainerBoxKey.load_or_create(str(tmp_path / "b.key"))
    pkg = weights_crypto.seal(b"secret", box.pubkey_hex)
    with pytest.raises(Exception):
        weights_crypto.unseal(pkg, other)
