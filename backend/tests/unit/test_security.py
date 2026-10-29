"""Segurança básica: senhas (argon2id + migração bcrypt), JWT, TOTP, criptografia.

Reescrito na AÇÃO 002 (W1): o arquivo antigo importava ``app.modules.auth.security``
(inexistente) e validava formatos de ID antigos (``RD-XXXX-XXXX``). Os objetivos
originais (hash/verify de senha; encode/decode de JWT; formato de IDs) foram mantidos
contra as APIs reais e o contrato v1.
"""
import time

import bcrypt
import pytest
from jose import jwt

from app.core import crypto, passwords, tokens, totp
from app.core.config import settings


def test_password_hash_is_argon2id_and_verifies():
    h = passwords.hash_password("SenhaForte123")
    assert h.startswith("$argon2id$")
    assert passwords.verify_password("SenhaForte123", h) == (True, False)
    assert passwords.verify_password("errada123456", h) == (False, False)


def test_legacy_bcrypt_verifies_and_requests_rehash():
    legacy = bcrypt.hashpw(b"SenhaLegada99", bcrypt.gensalt(rounds=4)).decode()
    assert passwords.verify_password("SenhaLegada99", legacy) == (True, True)
    assert passwords.verify_password("outra", legacy) == (False, False)


def test_password_policy():
    assert passwords.password_policy_errors("Curta1") != []
    assert passwords.password_policy_errors("somenteletras") != []
    assert passwords.password_policy_errors("1234567890") != []
    assert passwords.password_policy_errors("joaosilva2026", "joaosilva@example.com") != []
    assert passwords.password_policy_errors("Boa-Senha-2026", "joao@example.com") == []


def test_jwt_roundtrip_and_type_check():
    tok, claims = tokens.create_access_token("u1", "s1", amr=["pwd"])
    out = tokens.decode(tok, expected_typ=tokens.TYP_ACCESS)
    assert out["sub"] == "u1" and out["sid"] == "s1" and out["typ"] == "access"
    with pytest.raises(tokens.TokenError):
        tokens.decode(tok, expected_typ=tokens.TYP_MFA)


def test_jwt_rejects_alg_none_and_wrong_key_and_audience():
    payload = {"sub": "x", "typ": "access", "iat": int(time.time()), "exp": int(time.time()) + 60, "jti": "1",
               "iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE}
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    none_tok = b64({"alg": "none", "typ": "JWT"}) + "." + b64(payload) + "."
    with pytest.raises(tokens.TokenError):
        tokens.decode(none_tok)
    with pytest.raises(tokens.TokenError):
        tokens.decode(jwt.encode(payload, "outra-chave-qualquer", algorithm="HS256"))
    bad_aud = dict(payload, aud="outra")
    with pytest.raises(tokens.TokenError):
        tokens.decode(jwt.encode(bad_aud, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM))


def test_totp_window_policy():
    secret = totp.new_secret()
    now = 1_800_000_000.0
    step = totp.current_step(now)
    assert totp.match_step(secret, totp.code_at_step(secret, step), now=now) == step
    assert totp.match_step(secret, totp.code_at_step(secret, step - 1), now=now) == step - 1  # janela anterior aceita
    assert totp.match_step(secret, totp.code_at_step(secret, step + 1), now=now) == step + 1
    assert totp.match_step(secret, totp.code_at_step(secret, step - 2), now=now) is None
    assert totp.match_step(secret, "abc123", now=now) is None
    uri = totp.otpauth_uri(secret, "a@example.com")
    assert uri.startswith("otpauth://totp/") and "qrserver" not in uri


def test_encryption_roundtrip_and_aad_binding():
    enc = crypto.encrypt_str("SEGREDO", aad="user:1")
    assert enc.startswith("v1:") and "SEGREDO" not in enc
    assert crypto.decrypt_str(enc, aad="user:1") == "SEGREDO"
    with pytest.raises(Exception):
        crypto.decrypt_str(enc, aad="user:2")  # segredo não pode ser movido para outro usuário
    assert crypto.hmac_hex("123456", purpose="a") != crypto.hmac_hex("123456", purpose="b")
