from substrateinterface import Keypair

from omakase_maintainer import identity


def _payload(kp):
    p = {"competition": "omakase-router", "hotkey": kp.ss58_address, "github_login": "alice",
         "weights_sha256": "a" * 64}
    p["signature"] = identity.sign_payload(kp, p)
    return p


def test_valid_signature_accepted():
    kp = Keypair.create_from_uri("//MinerAlice")
    assert identity.verify_signature(_payload(kp))


def test_tampered_artifact_rejected():
    kp = Keypair.create_from_uri("//MinerAlice")
    p = _payload(kp)
    p["weights_sha256"] = "b" * 64  # changed after signing
    assert not identity.verify_signature(p)


def test_wrong_hotkey_rejected():
    kp = Keypair.create_from_uri("//MinerAlice")
    p = _payload(kp)
    p["hotkey"] = Keypair.create_from_uri("//MinerMallory").ss58_address
    assert not identity.verify_signature(p)


def test_ed25519_also_supported():
    # ed25519 has no derivation paths in substrate-interface; make from a raw seed
    kp = Keypair.create_from_seed("0x" + "11" * 32, crypto_type=identity.ED25519)
    assert identity.verify_signature(_payload(kp))
