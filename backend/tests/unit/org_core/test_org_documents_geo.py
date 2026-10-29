"""W-ORG — validação de CPF/CNPJ (inclui CNPJ alfanumérico) e geografia/fuso do Brasil."""
import pytest

from app.core.timeutils import validate_timezone
from app.modules.establishments import geo
from app.modules.organizations import documents as D


# ─── CPF ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cpf", ["529.982.247-25", "52998224725", "111.444.777-35"])
def test_cpf_valid(cpf):
    assert D.is_valid_cpf(cpf)
    doc, t = D.normalize_document(cpf)
    assert t == "CPF" and doc == D.strip_mask(cpf) and len(doc) == 11


@pytest.mark.parametrize("cpf", ["529.982.247-24", "111.111.111-11", "00000000000", "1234567890", "5299822472A", ""])
def test_cpf_invalid(cpf):
    assert not D.is_valid_cpf(cpf)
    with pytest.raises(D.DocumentError):
        D.normalize_document(cpf, "CPF")


def test_make_cpf_produces_valid_and_every_wrong_check_digit_fails():
    base = "123456789"
    good = D.make_cpf(base)
    assert D.is_valid_cpf(good)
    for d in "0123456789":
        cand = good[:10] + d
        assert D.is_valid_cpf(cand) == (cand == good)


# ─── CNPJ ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cnpj", ["11.222.333/0001-81", "11222333000181", "04.252.011/0001-10"])
def test_cnpj_numeric_valid(cnpj):
    assert D.is_valid_cnpj(cnpj)
    doc, t = D.normalize_document(cnpj)
    assert t == "CNPJ" and doc == D.strip_mask(cnpj)


def test_cnpj_alphanumeric_official_example():
    # Exemplo publicado pela Receita Federal para o CNPJ alfanumérico (IN RFB 2.229/2024)
    assert D.is_valid_cnpj("12.ABC.345/01DE-35")
    assert D.normalize_document("12.abc.345/01de-35") == ("12ABC34501DE35", "CNPJ")
    assert not D.is_valid_cnpj("12.ABC.345/01DE-36")


@pytest.mark.parametrize("cnpj", ["11.222.333/0001-80", "00000000000000", "AAAAAAAAAAAAAA", "1122233300018",
                                  "11222333000181X", "12ABC34501DEA5"])
def test_cnpj_invalid(cnpj):
    assert not D.is_valid_cnpj(cnpj)


def test_make_cnpj_alphanumeric_roundtrip():
    for base in ("AB1CD2EF3GH4", "000000000001", "ZZZZZZZZZZZZ"):
        c = D.make_cnpj(base)
        assert D.is_valid_cnpj(c)
        assert not D.is_valid_cnpj(c[:-1] + str((int(c[-1]) + 1) % 10))


def test_document_type_mismatch_and_unknown_length():
    with pytest.raises(D.DocumentError):
        D.normalize_document("11222333000181", "CPF")
    with pytest.raises(D.DocumentError):
        D.normalize_document("123456")


def test_mask_document_hides_cpf():
    assert D.mask_document("52998224725", "CPF") == "***.***.***-25"
    assert D.format_document("11222333000181", "CNPJ") == "11.222.333/0001-81"


# ─── Geografia / fuso ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "uf,city,tz",
    [
        ("AC", "Rio Branco", "America/Rio_Branco"),
        ("AC", "Cruzeiro do Sul", "America/Rio_Branco"),
        ("AM", "Manaus", "America/Manaus"),
        ("AM", "Eirunepé", "America/Eirunepe"),
        ("AM", "Tabatinga", "America/Eirunepe"),
        ("SP", "São Paulo", "America/Sao_Paulo"),
        ("GO", "Goiânia", "America/Sao_Paulo"),
        ("MT", "Cuiabá", "America/Cuiaba"),
        ("MS", "Campo Grande", "America/Campo_Grande"),
        ("RO", "Porto Velho", "America/Porto_Velho"),
        ("RR", "Boa Vista", "America/Boa_Vista"),
        ("PE", "Recife", "America/Recife"),
        ("PE", "Fernando de Noronha", "America/Noronha"),
        ("BA", "Salvador", "America/Bahia"),
        ("TO", "Palmas", "America/Araguaina"),
    ],
)
def test_timezone_suggestion(uf, city, tz):
    s = geo.suggest_timezone(uf, city)
    assert s["timezone"] == tz
    assert validate_timezone(s["timezone"]) == tz  # zona existe no tzdata


def test_every_uf_has_valid_zone_and_region():
    from app.core.ids import BR_UFS

    for uf in BR_UFS:
        assert validate_timezone(geo.UF_TIMEZONE[uf])
        assert geo.UF_REGION[uf] in geo.REGIONS


def test_invalid_uf_rejected():
    with pytest.raises(ValueError):
        geo.suggest_timezone("XX")
    with pytest.raises(ValueError):
        geo.derive_keys("ZZ", "Cidade", None)


def test_derive_keys_matches_rbac_city_key():
    from app.core.authz import city_key

    k = geo.derive_keys("go", "Goiânia", "Setor Bueno")
    assert k == {"state": "GO", "city_key": city_key("GO", "Goiânia"), "region": "CENTRO_OESTE",
                 "neighborhood_key": "setor-bueno"}
    assert k["city_key"] == "GO:goiania"


def test_postal_code_and_haversine():
    assert geo.normalize_postal_code("74.230-010") == "74230010"
    assert geo.normalize_postal_code(None) is None
    with pytest.raises(ValueError):
        geo.normalize_postal_code("7423-01")
    # Goiânia (Praça Cívica) → Anápolis ≈ 50 km
    d = geo.haversine_m(-16.6799, -49.2550, -16.3281, -48.9534)
    assert 45_000 < d < 55_000
    assert geo.haversine_m(-16.68, -49.25, -16.68, -49.25) == 0


def test_invalid_iana_rejected():
    for bad in ("America/Goiania_City", "GMT-3x", "", "Brasil/Brasilia"):
        with pytest.raises(ValueError):
            validate_timezone(bad)
