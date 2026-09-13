import hashlib
import json
import re
from pathlib import Path

import pytest

import xfeed.opera_pairing as pairing_module
from xfeed.opera_pairing import PairingError, PairingStore


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_pairing_exchanges_single_use_code_for_persisted_token_digest(tmp_path):
    clock = FakeClock(100.0)
    store = PairingStore(tmp_path / "pairing.json", clock=clock)
    offer = store.issue()
    token = store.exchange(offer.code)

    assert len(offer.code) == 8
    assert offer.expires_at == 220.0
    assert len(token) >= 43
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    payload = json.loads((tmp_path / "pairing.json").read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }
    assert len(payload["token_sha256"]) == 64
    assert PairingStore(tmp_path / "pairing.json", clock=clock).authenticate(token)
    with pytest.raises(PairingError):
        store.exchange(offer.code)


def test_pairing_code_expires_after_two_minutes(tmp_path):
    clock = FakeClock()
    store = PairingStore(tmp_path / "pairing.json", clock=clock)
    offer = store.issue()
    clock.advance(120)

    with pytest.raises(PairingError):
        store.exchange(offer.code)


def test_pairing_code_stops_accepting_attempts_after_five_failures(tmp_path):
    store = PairingStore(tmp_path / "pairing.json", clock=FakeClock())
    offer = store.issue()

    for _ in range(5):
        with pytest.raises(PairingError):
            store.exchange("AAAAAAAA")

    with pytest.raises(PairingError):
        store.exchange(offer.code)


def test_unicode_pairing_codes_consume_attempt_budget_without_type_errors(tmp_path):
    store = PairingStore(tmp_path / "pairing.json", clock=FakeClock())
    offer = store.issue()

    for _ in range(5):
        with pytest.raises(PairingError):
            store.exchange("AAAAAAAé")

    with pytest.raises(PairingError):
        store.exchange(offer.code)


def test_revoke_removes_persisted_authentication(tmp_path):
    path = tmp_path / "pairing.json"
    store = PairingStore(path, clock=FakeClock())
    token = store.exchange(store.issue().code)

    store.revoke()

    assert not path.exists()
    assert not store.authenticate(token)
    assert not PairingStore(path, clock=FakeClock()).authenticate(token)


def test_corrupt_persisted_pairing_state_never_authenticates(tmp_path):
    path = tmp_path / "pairing.json"
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(PairingError):
        PairingStore(path, clock=FakeClock()).authenticate("attacker-token")


def test_boolean_persisted_version_never_authenticates(tmp_path):
    path = tmp_path / "pairing.json"
    token = "known-token"
    path.write_text(
        json.dumps(
            {
                "version": True,
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PairingError):
        PairingStore(path, clock=FakeClock()).authenticate(token)


def test_pairing_read_failure_raises_without_authorizing(tmp_path, monkeypatch):
    path = tmp_path / "pairing.json"
    path.write_text("{}", encoding="utf-8")

    def fail_read(_path, *args, **kwargs):
        raise OSError("read failed")

    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(PairingError):
        PairingStore(path, clock=FakeClock()).authenticate("attacker-token")


def test_pairing_write_failure_never_authorizes_generated_token(tmp_path, monkeypatch):
    path = tmp_path / "pairing.json"
    store = PairingStore(path, clock=FakeClock())
    generated_token = "generated-token"
    monkeypatch.setattr(pairing_module.secrets, "token_urlsafe", lambda _size: generated_token)

    def fail_dump(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(pairing_module.json, "dump", fail_dump)

    with pytest.raises(PairingError):
        store.exchange(store.issue().code)

    assert not path.exists()
    assert not store.authenticate(generated_token)


def test_atomic_replace_failure_preserves_old_token_and_rejects_new_token(tmp_path, monkeypatch):
    path = tmp_path / "pairing.json"
    store = PairingStore(path, clock=FakeClock())
    old_token = store.exchange(store.issue().code)
    new_token = "replacement-token"
    monkeypatch.setattr(pairing_module.secrets, "token_urlsafe", lambda _size: new_token)

    def fail_replace(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(pairing_module.os, "replace", fail_replace)

    with pytest.raises(PairingError):
        store.exchange(store.issue().code)

    assert store.authenticate(old_token)
    assert not store.authenticate(new_token)


def test_revoke_failure_is_reported_and_never_claimed_as_success(tmp_path, monkeypatch):
    path = tmp_path / "pairing.json"
    store = PairingStore(path, clock=FakeClock())
    token = store.exchange(store.issue().code)

    def fail_unlink(*args, **kwargs):
        raise OSError("unlink failed")

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(PairingError):
        store.revoke()

    assert store.authenticate(token)
