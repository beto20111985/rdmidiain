"""Motor de conflitos PURO — todos os exemplos de A6 §10.7 / ADR_006 #11 (sem banco)."""
import uuid

from app.modules.conflicts import engine as E


def u() -> uuid.UUID:
    return uuid.uuid4()


class Tax:
    """Árvores mínimas: ALIMENTAÇÃO > {Supermercado, Distribuidora de bebidas}; segmentos
    bebidas > alcoólicas > cerveja; alimentos > snacks; apostas."""

    def __init__(self):
        self.sector = u()
        self.supermercado = u()
        self.distribuidora = u()
        self.academia_sector = u()
        self.academia = u()
        self.kinds = {self.sector: "SECTOR", self.supermercado: "CATEGORY", self.distribuidora: "CATEGORY",
                      self.academia_sector: "SECTOR", self.academia: "CATEGORY"}
        self.bebidas = u()
        self.alcoolicas = u()
        self.cerveja = u()
        self.alimentos = u()
        self.snacks = u()
        self.apostas = u()
        self.seg_paths = {
            self.bebidas: (self.bebidas,),
            self.alcoolicas: (self.bebidas, self.alcoolicas),
            self.cerveja: (self.bebidas, self.alcoolicas, self.cerveja),
            self.alimentos: (self.alimentos,),
            self.snacks: (self.alimentos, self.snacks),
            self.apostas: (self.apostas,),
        }

    def node(self, nid):
        path = {self.supermercado: (self.sector, self.supermercado),
                self.distribuidora: (self.sector, self.distribuidora),
                self.academia: (self.academia_sector, self.academia),
                self.sector: (self.sector,)}[nid]
        return E.NodeRef(id=nid, kind=self.kinds[nid], path_ids=path), tuple((x, self.kinds[x]) for x in path)

    def segs(self, *ids):
        return tuple(E.SegRef(id=i, path_ids=self.seg_paths[i]) for i in ids)

    def ad(self, org, cat, *segs, brand=None, product=None, campaign=None):
        n, k = self.node(cat)
        return E.AdFacts(advertiser_org_id=org, advertiser_category=n, advertiser_category_kinds=k,
                         segments=self.segs(*segs), segments_source="CREATIVE", brand_id=brand, product_id=product,
                         campaign_id=campaign or u(), creative_id=u())

    def est(self, org, cat, *segs):
        n, k = self.node(cat)
        return E.EstFacts(establishment_id=u(), org_id=org, category=n, category_kinds=k, segments=self.segs(*segs))


def test_distribuidora_x_distribuidora_bloqueado_mesmo_ramo():
    t = Tax()
    d = E.decide(t.ad(u(), t.distribuidora, t.bebidas), t.est(u(), t.distribuidora, t.bebidas))
    assert (d.result, d.reason_code) == ("BLOCK", "MESMO_RAMO")
    assert d.rule_path[-1]["step"] == 6


def test_supermercado_anuncio_bebida_em_distribuidora_bloqueado_pelo_segmento_do_anuncio():
    t = Tax()
    d = E.decide(t.ad(u(), t.supermercado, t.cerveja), t.est(u(), t.distribuidora, t.bebidas))
    assert (d.result, d.reason_code) == ("BLOCK", "SEGMENTO")
    assert d.rule_path[-1]["detail"]["ad_segment_id"] == str(t.cerveja)


def test_supermercado_anuncio_salgadinho_em_distribuidora_permitido():
    t = Tax()
    d = E.decide(t.ad(u(), t.supermercado, t.snacks), t.est(u(), t.distribuidora, t.bebidas))
    assert (d.result, d.reason_code) == ("ALLOW", "SEM_CONFLITO")
    # todos os passos foram avaliados e registrados (explicável)
    assert [p["step"] for p in d.rule_path] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_override_admin_permite_conflito_de_segmento():
    t = Tax()
    ov = E.OverrideFact(id=u(), justification="Parceria cruzada aprovada pela diretoria em 07/10/2026")
    d = E.decide(t.ad(u(), t.supermercado, t.cerveja), t.est(u(), t.distribuidora, t.bebidas), override=ov)
    assert (d.result, d.reason_code) == ("ALLOW", "OVERRIDE_ADMIN")


def test_apostas_sempre_bloqueado_mesmo_com_override_excecao_e_regra_allow():
    t = Tax()
    ad = t.ad(u(), t.supermercado, t.apostas)
    est = t.est(u(), t.academia)
    ov = E.OverrideFact(id=u(), justification="x" * 30)
    exc = E.ExceptionFact(id=u(), object_type="ADVERTISER", object_id=ad.advertiser_org_id)
    allow_rule = E.RuleFact(id=u(), subject_type="ESTABLISHMENT", subject_id=est.establishment_id,
                            object_type="AD_ADVERTISER", object_id=ad.advertiser_org_id, include_descendants=False,
                            effect="ALLOW", specificity=E.specificity_for("ESTABLISHMENT", None, "AD_ADVERTISER", None))
    d = E.decide(ad, est, blocked_policy_segment_ids={t.apostas}, override=ov, exceptions=[exc], rules=[allow_rule])
    assert (d.result, d.reason_code) == ("BLOCK", "PROIBIDO_UNIVERSAL")


def test_politica_bloqueia_por_ancestral():
    t = Tax()
    d = E.decide(t.ad(u(), t.supermercado, t.cerveja), t.est(u(), t.academia), blocked_policy_segment_ids={t.bebidas})
    assert d.reason_code == "PROIBIDO_UNIVERSAL"


def test_excecao_do_estabelecimento_so_para_a_marca():
    t = Tax()
    marca_z, outra = u(), u()
    est = t.est(u(), t.distribuidora, t.bebidas)
    exc = E.ExceptionFact(id=u(), object_type="BRAND", object_id=marca_z)
    ok = E.decide(t.ad(u(), t.supermercado, t.cerveja, brand=marca_z), est, exceptions=[exc])
    no = E.decide(t.ad(u(), t.supermercado, t.cerveja, brand=outra), est, exceptions=[exc])
    assert (ok.result, ok.reason_code) == ("ALLOW", "EXCECAO_ESTABELECIMENTO")
    assert (no.result, no.reason_code) == ("BLOCK", "SEGMENTO")


def test_mesma_rede_permitida_e_configuravel():
    t = Tax()
    org = u()
    ad = t.ad(org, t.distribuidora, t.bebidas)
    est = t.est(org, t.distribuidora, t.bebidas)
    assert E.decide(ad, est).reason_code == "MESMA_REDE"
    d = E.decide(ad, est, same_network_allow=False)
    assert (d.result, d.reason_code) == ("BLOCK", "MESMO_RAMO")


def test_empate_de_regras_explicitas_bloqueia():
    t = Tax()
    ad = t.ad(u(), t.academia, t.snacks)
    est = t.est(u(), t.supermercado)
    spec = E.specificity_for("EST_CATEGORY", "CATEGORY", "AD_SEGMENT", 1)
    r1 = E.RuleFact(id=u(), subject_type="EST_CATEGORY", subject_id=t.supermercado, object_type="AD_SEGMENT",
                    object_id=t.snacks, include_descendants=True, effect="ALLOW", specificity=spec)
    r2 = E.RuleFact(id=u(), subject_type="EST_CATEGORY", subject_id=t.supermercado, object_type="AD_SEGMENT",
                    object_id=t.snacks, include_descendants=True, effect="BLOCK", specificity=spec)
    d = E.decide(ad, est, rules=[r1, r2])
    assert (d.result, d.reason_code) == ("BLOCK", "REGRA_EXPLICITA")
    assert d.rule_path[-1]["detail"]["tie"] is True
    # sem empate, a ALLOW sozinha libera
    assert E.decide(ad, est, rules=[r1]).result == "ALLOW"


def test_regra_mais_especifica_vence_e_descendentes():
    t = Tax()
    ad = t.ad(u(), t.academia, t.cerveja)
    est = t.est(u(), t.supermercado)  # vende nada declarado
    block_bebidas = E.RuleFact(id=u(), subject_type="EST_CATEGORY", subject_id=t.sector, object_type="AD_SEGMENT",
                               object_id=t.bebidas, include_descendants=True, effect="BLOCK",
                               specificity=E.specificity_for("EST_CATEGORY", "SECTOR", "AD_SEGMENT", 0))
    allow_cerveja = E.RuleFact(id=u(), subject_type="EST_CATEGORY", subject_id=t.sector, object_type="AD_SEGMENT",
                               object_id=t.cerveja, include_descendants=True, effect="ALLOW",
                               specificity=E.specificity_for("EST_CATEGORY", "SECTOR", "AD_SEGMENT", 2))
    assert E.decide(ad, est, rules=[block_bebidas]).result == "BLOCK"  # cerveja ⊂ bebidas, setor ⊃ supermercado
    d = E.decide(ad, est, rules=[block_bebidas, allow_cerveja])
    assert (d.result, d.reason_code) == ("ALLOW", "REGRA_EXPLICITA")
    # sem include_descendants a regra no setor não casa com o supermercado
    exact = E.RuleFact(id=u(), subject_type="EST_CATEGORY", subject_id=t.sector, object_type="AD_SEGMENT",
                       object_id=t.bebidas, include_descendants=False, effect="BLOCK", specificity=1)
    assert E.decide(ad, est, rules=[exact]).reason_code == "SEM_CONFLITO"


def test_concorrente_explicito_bloqueia():
    t = Tax()
    d = E.decide(t.ad(u(), t.academia, t.snacks), t.est(u(), t.supermercado), competitor_link=True)
    assert (d.result, d.reason_code) == ("BLOCK", "CONCORRENTE")


def test_distribuidora_anunciando_em_supermercado_bloqueado():
    t = Tax()
    d = E.decide(t.ad(u(), t.distribuidora, t.bebidas), t.est(u(), t.supermercado, t.alimentos, t.bebidas))
    assert (d.result, d.reason_code) == ("BLOCK", "SEGMENTO")


def test_supermercado_x_academia_mesmo_setor_diferente_nao_e_mesmo_ramo():
    t = Tax()
    # supermercado e distribuidora compartilham só o SETOR → não é mesmo ramo
    d = E.decide(t.ad(u(), t.supermercado, t.snacks), t.est(u(), t.distribuidora))
    assert d.reason_code == "SEM_CONFLITO"


def test_anuncio_x_anuncio_concorrentes():
    t = Tax()
    a = t.ad(u(), t.supermercado, t.cerveja)
    b = t.ad(u(), t.distribuidora, t.bebidas)
    c = t.ad(u(), t.academia, t.snacks)
    assert E.ad_vs_ad(a, b).result == "BLOCK"  # cerveja ⊂ bebidas
    assert E.ad_vs_ad(a, c).result == "ALLOW"
    same = t.ad(a.advertiser_org_id, t.supermercado, t.cerveja)
    assert E.ad_vs_ad(a, same).reason_code == "MESMO_ANUNCIANTE"
    assert E.ad_vs_ad(a, c, competitor_link=True).reason_code == "CONCORRENTE"
    d1 = t.ad(u(), t.distribuidora, t.snacks)
    d2 = t.ad(u(), t.distribuidora, t.cerveja)
    assert E.ad_vs_ad(d1, d2).reason_code == "MESMO_RAMO"


def test_decisao_deterministica_e_hash_estavel():
    t = Tax()
    ad = t.ad(u(), t.supermercado, t.cerveja)
    est = t.est(u(), t.distribuidora, t.bebidas)
    a = E.decide(ad, est)
    b = E.decide(ad, est)
    assert a.facts_hash == b.facts_hash and a.rule_path == b.rule_path
    assert len(a.facts_hash) == 64


def test_combine_campanha_bloqueia_se_qualquer_criativo_bloquear():
    t = Tax()
    est = t.est(u(), t.distribuidora, t.bebidas)
    ok = E.decide(t.ad(u(), t.supermercado, t.snacks), est)
    bad = E.decide(t.ad(u(), t.supermercado, t.cerveja), est)
    assert E.combine([(u(), ok), (u(), bad)]).result == "BLOCK"
    assert E.combine([(u(), ok)]).result == "ALLOW"


def test_regra_por_marca_e_por_produto():
    t = Tax()
    marca, produto = u(), u()
    est = t.est(u(), t.academia)
    spec_brand = E.specificity_for("ESTABLISHMENT", None, "AD_BRAND", None)
    spec_prod = E.specificity_for("ESTABLISHMENT", None, "AD_PRODUCT", None)
    block_brand = E.RuleFact(id=u(), subject_type="ESTABLISHMENT", subject_id=est.establishment_id,
                             object_type="AD_BRAND", object_id=marca, include_descendants=False, effect="BLOCK",
                             specificity=spec_brand)
    allow_prod = E.RuleFact(id=u(), subject_type="ESTABLISHMENT", subject_id=est.establishment_id,
                            object_type="AD_PRODUCT", object_id=produto, include_descendants=False, effect="ALLOW",
                            specificity=spec_prod)
    ad = t.ad(u(), t.supermercado, t.snacks, brand=marca)
    d = E.decide(ad, est, rules=[block_brand])
    assert (d.result, d.reason_code) == ("BLOCK", "REGRA_EXPLICITA")  # bloqueio de marca (ETAPA2 #97)
    other = t.ad(u(), t.supermercado, t.snacks, brand=u())
    assert E.decide(other, est, rules=[block_brand]).result == "ALLOW"
    # produto é mais específico que marca → a liberação do produto vence o bloqueio da marca
    ad_p = t.ad(u(), t.supermercado, t.snacks, brand=marca, product=produto)
    assert spec_prod > spec_brand
    assert E.decide(ad_p, est, rules=[block_brand, allow_prod]).result == "ALLOW"
