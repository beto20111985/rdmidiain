"""Fail-fast de configuração no startup da API/worker/beat fora de development/test."""
import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.core import startup_checks as sc
from tests.unit.test_config import _settings


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _spki_b64u(key) -> str:
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.urlsafe_b64encode(der).rstrip(b"=").decode()


P256 = ec.generate_private_key(ec.SECP256R1())
P384 = ec.generate_private_key(ec.SECP384R1())
RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _prod(monkeypatch, tmp_path, **env):
    return _settings(monkeypatch, ENVIRONMENT="production", **env)


def test_valid_runtime_config(monkeypatch, tmp_path):
    keyfile = tmp_path / "manifest.pem"
    keyfile.write_text(_pem(P256), encoding="utf-8")
    rel = tmp_path / "rel.json"
    rel.write_text('{"rel-2026-10": "%s"}' % _spki_b64u(P256), encoding="utf-8")
    s = _prod(monkeypatch, tmp_path, MANIFEST_SIGNING_KEY_FILE=str(keyfile), PLAYER_RELEASE_PUBLIC_KEYS_FILE=str(rel))
    assert sc.runtime_problems(s) == []
    sc.enforce_runtime_config("api", s)


def test_missing_manifest_key_fails_fast_without_values(monkeypatch, tmp_path):
    s = _prod(monkeypatch, tmp_path)
    with pytest.raises(sc.StartupConfigError) as ei:
        sc.enforce_runtime_config("api", s)
    msg = str(ei.value)
    assert "MANIFEST_SIGNING_KEY(_FILE) ausente" in msg and "ENVIRONMENT=production" in msg


@pytest.mark.parametrize("bad", ["lixo", _pem(P384), _pem(RSA)], ids=["nao-pem", "p384", "rsa"])
def test_manifest_key_must_be_p256(monkeypatch, tmp_path, bad):
    keyfile = tmp_path / "manifest.pem"
    keyfile.write_text(bad, encoding="utf-8")
    s = _prod(monkeypatch, tmp_path, MANIFEST_SIGNING_KEY_FILE=str(keyfile))
    probs = sc.runtime_problems(s)
    assert len(probs) == 1 and probs[0].startswith("MANIFEST_SIGNING_KEY")
    assert "BEGIN" not in probs[0] and "lixo" not in probs[0]  # sem valores


def test_unreadable_files_fail_at_settings(monkeypatch, tmp_path):
    from app.core.config import ConfigurationError

    for var in ("MANIFEST_SIGNING_KEY_FILE", "PLAYER_RELEASE_PUBLIC_KEYS_FILE"):
        with pytest.raises(ConfigurationError) as ei:
            _prod(monkeypatch, tmp_path, **{var: str(tmp_path / "nao-existe")})
        assert var in str(ei.value)


def test_release_keys_must_be_p256_spki(monkeypatch, tmp_path):
    keyfile = tmp_path / "manifest.pem"
    keyfile.write_text(_pem(P256), encoding="utf-8")
    rel = tmp_path / "rel.json"
    rel.write_text('{"boa": "%s", "rsa": "%s", "lixo": "abc"}' % (_spki_b64u(P256), _spki_b64u(RSA)), encoding="utf-8")
    s = _prod(monkeypatch, tmp_path, MANIFEST_SIGNING_KEY_FILE=str(keyfile), PLAYER_RELEASE_PUBLIC_KEYS_FILE=str(rel))
    probs = sc.runtime_problems(s)
    assert sorted(probs) == ["PLAYER_RELEASE_PUBLIC_KEYS[lixo] não é chave pública SPKI P-256",
                             "PLAYER_RELEASE_PUBLIC_KEYS[rsa] não é chave pública SPKI P-256"]


def test_staging_is_checked_and_dev_test_are_not(monkeypatch, tmp_path):
    s = _settings(monkeypatch, ENVIRONMENT="staging", PLAYER_ALLOWED_ORIGINS="http://player.x.com.br")
    probs = sc.runtime_problems(s)
    assert "MANIFEST_SIGNING_KEY(_FILE) ausente" in probs
    assert "PLAYER_ALLOWED_ORIGINS deve conter somente origens https://" in probs
    for env in ("development", "test"):
        assert sc.runtime_problems(_settings(monkeypatch, ENVIRONMENT=env)) == []


def test_worker_and_beat_abort_on_bad_config(monkeypatch, tmp_path):
    from app.workers import celery_app as ca

    s = _prod(monkeypatch, tmp_path)
    monkeypatch.setattr(sc, "_settings", s)
    for fn in (ca._check_config_worker, ca._check_config_beat):
        with pytest.raises(SystemExit) as ei:
            fn()
        assert "MANIFEST_SIGNING_KEY" in str(ei.value)


async def test_api_lifespan_refuses_to_start(monkeypatch, tmp_path):
    from app.main import app, lifespan

    s = _prod(monkeypatch, tmp_path)
    monkeypatch.setattr(sc, "_settings", s)
    with pytest.raises(sc.StartupConfigError):
        async with lifespan(app):
            pass
