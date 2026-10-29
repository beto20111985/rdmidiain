"""Migration 0006 (W-FIN — termos de comissão POR PAGAMENTO + peso por tela) — AÇÃO 002 correção (h)/(i).

Bancos DESCARTÁVEIS (rdtest_mig_*). Cenários:
- round-trip head → 0005 → head: estrutura da 0005 idêntica à de um banco recém-migrado até a 0005
  e head reconstruído idêntico (drift/catálogo do head = create_all coberto em test_migrations_0004);
- upgrade COM DADOS no formato 0005 equivalentes ao banco de dev (campanha com cronograma de
  receita, telas com plano congelado, acúmulo, liberação de pool; campanha sem cronograma):
  cada linha vira o termo INITIAL da sua cobrança, telas/acúmulos/liberações ligados ao termo,
  plano diário + plano do dia já apropriado com os MESMOS números, auditoria encadeada íntegra;
- ``commission_day_plans`` é append-only (trigger) após a migração;
- downgrade recusa quando há mais de um termo por campanha (renovação/alteração paga) e mantém a
  revisão; sem termos extras, volta ao formato 0005 (acúmulos/liberações religados à campanha) e
  o upgrade seguinte reconverte.
"""
import json

import pytest

from tests.migrations.test_migrations import alembic, q, scratch_db
from tests.migrations.test_migrations_0004 import FULL_CATALOG_SQL, diff

CHAIN_BAD_SQL = (
    "with x as (select a.seq, a.prev_hash, a.row_hash, lag(a.row_hash) over (order by a.seq) p, "
    "encode(sha256(convert_to(rd_audit_hash_material(a, a.prev_hash), 'UTF8')), 'hex') c "
    "from audit_logs a) select count(*) filter (where c <> row_hash or prev_hash <> "
    "coalesce(p, repeat('0', 64))) from x"
)


def test_round_trip_head_to_0005_and_back(_test_database):
    with scratch_db() as ref0005, scratch_db() as db:
        alembic(ref0005, "upgrade", "0005")
        alembic(db, "upgrade", "head")
        head_catalog = q(db, FULL_CATALOG_SQL)
        alembic(db, "downgrade", "0005")
        assert q(db, "select version_num from alembic_version")[0][0] == "0005"
        only_down, only_ref = diff(q(db, FULL_CATALOG_SQL), q(ref0005, FULL_CATALOG_SQL))
        assert only_down == [] and only_ref == [], (only_down, only_ref)
        alembic(db, "upgrade", "head")
        assert q(db, FULL_CATALOG_SQL) == head_catalog


def _insert_0005_commission_data(db) -> dict:
    """Formato 0005: commission_campaigns PK campaign_id, plano congelado por tela."""
    ids = {}
    ids["org"] = q(db, "insert into organizations (id,name,document,document_type,is_active) values "
                       "(gen_random_uuid(),'Anunciante','11222333000181','CNPJ',true) returning id")[0][0]
    for key in ("order1", "order2"):
        ids[key] = q(db, "insert into payment_orders (id,org_id,reference_type,reference_id,reference,items,amount_cents,"
                         "eligible_revenue_cents,services_cents,method,status,provider,instance_slug) values "
                         "(gen_random_uuid(), :o, 'campaign', :r, '{}'::jsonb, '[]'::jsonb, 100000, 100000, 0, "
                         "'pix', 'PAID', 'fake', 'sandbox') returning id", o=ids["org"], r=f"REF-{key}")[0][0]
    ids["camp1"] = q(db, "select gen_random_uuid()")[0][0]
    ids["camp2"] = q(db, "select gen_random_uuid()")[0][0]
    ids["s1"], ids["s2"], ids["s3"], ids["est"] = (q(db, "select gen_random_uuid()")[0][0] for _ in range(4))
    q(db, "insert into revenue_schedules (id,payment_order_id,campaign_id,org_id,eligible_cents,start_date,end_date,status) "
          "values (gen_random_uuid(), :p, :c, :o, 100000, '2026-09-01', '2026-09-30', 'ACTIVE')",
      p=ids["order1"], c=ids["camp1"], o=ids["org"])
    for camp, order in (("camp1", "order1"), ("camp2", "order2")):
        q(db, "insert into commission_campaigns (campaign_id,payment_order_id,org_id,model,pool_bps,eligible_revenue_cents,"
              "pool_cents,status,snapshot) values (:c, :p, :o, 'A_POOL', 500, 100000, 5000, 'ACTIVE', '{}'::jsonb)",
          c=ids[camp], p=ids[order], o=ids["org"])
    screens = (
        ("camp1", "s1", 1000, {"2026-09-01": 10, "2026-09-02": 10}),
        ("camp1", "s2", 1500, {"2026-09-01": 5}),
        ("camp2", "s3", 1000, {"2026-10-03": 4, "2026-10-05": 4}),  # sem cronograma: período = plano
    )
    for camp, s, w, plan in screens:
        q(db, "insert into commission_campaign_screens (campaign_id,screen_id,establishment_id,establishment_org_id,"
              "receives_commission,weight_milli,rate_bps,attributed_revenue_cents,expected_total,share_ucents,plan) "
              "values (:c, :s, :e, NULL, true, :w, 0, 0, :t, 123, CAST(:p AS jsonb))",
          c=ids[camp], s=ids[s], e=ids["est"], w=w, t=sum(plan.values()), p=json.dumps(plan))
    q(db, "insert into commission_accruals (id,campaign_id,screen_id,establishment_id,local_day,competence,valid_count,"
          "expected_count,amount_ucents) values (gen_random_uuid(), :c, :s, :e, '2026-09-01', '2026-09-01', 10, 10, 777)",
      c=ids["camp1"], s=ids["s1"], e=ids["est"])
    q(db, "insert into commission_pool_releases (campaign_id,competence,released_cents) values (:c, '2026-09-01', 100)",
      c=ids["camp1"])
    return ids


def test_upgrade_with_0005_commission_rows_converts_and_audits(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "0005")
        ids = _insert_0005_commission_data(db)
        # AÇÃO 003: até a 0006 (não o head) — a recusa abaixo mantém a revisão em que o banco está
        alembic(db, "upgrade", "0006")

        terms = {r[0]: r[1:] for r in q(db, "select campaign_id, id, payment_order_id, kind, start_date::text, "
                                            "end_date::text, daily_plan, expected_total, pool_cents "
                                            "from commission_campaigns")}
        t1, t2 = terms[ids["camp1"]], terms[ids["camp2"]]
        assert t1[0] is not None and t2[0] is not None and t1[0] != t2[0]
        assert t1[1:] == (ids["order1"], "INITIAL", "2026-09-01", "2026-09-30",
                          {"2026-09-01": 15, "2026-09-02": 10}, 25, 5000)
        assert t2[1:] == (ids["order2"], "INITIAL", "2026-10-03", "2026-10-05",
                          {"2026-10-03": 4, "2026-10-05": 4}, 8, 5000)
        # telas ligadas ao termo; peso do estabelecimento preservado, peso de tela neutro (1000)
        assert sorted(q(db, "select terms_id, screen_id, weight_milli, establishment_weight_milli, screen_weight_milli "
                            "from commission_campaign_screens")) == sorted([
            (t1[0], ids["s1"], 1000, 1000, 1000), (t1[0], ids["s2"], 1500, 1500, 1000),
            (t2[0], ids["s3"], 1000, 1000, 1000)])
        cols = {r[0] for r in q(db, "select column_name from information_schema.columns "
                                    "where table_name = 'commission_campaign_screens'")}
        assert not cols & {"plan", "share_ucents", "expected_total"}
        # acúmulo e liberação religados ao termo, valores intactos
        assert q(db, "select terms_id, campaign_id, amount_ucents from commission_accruals") == [
            (t1[0], ids["camp1"], 777)]
        assert q(db, "select terms_id, competence::text, released_cents from commission_pool_releases") == [
            (t1[0], "2026-09-01", 100)]
        # plano do dia já apropriado = mesmos números do plano congelado antigo
        assert q(db, "select terms_id, local_day::text, plan, e_day from commission_day_plans") == [
            (t1[0], "2026-09-01", {str(ids["s1"]): 10, str(ids["s2"]): 5}, 15)]
        # auditoria da migração encadeada e íntegra
        audit = q(db, "select after_data from audit_logs where action = 'migration.0006.commission_terms_per_payment'")
        assert audit == [({"commission_campaigns": 2, "commission_campaign_screens": 3, "commission_accruals": 1,
                           "commission_pool_releases": 1, "commission_day_plans": 1},)]
        assert q(db, CHAIN_BAD_SQL)[0][0] == 0
        # plano do dia é append-only
        with pytest.raises(Exception) as ei:
            q(db, "update commission_day_plans set e_day = 0")
        assert "RD_APPEND_ONLY" in str(ei.value)
        # unicidade por (campanha, cobrança): a mesma cobrança não gera dois termos
        with pytest.raises(Exception) as ei:
            q(db, "insert into commission_campaigns (id,campaign_id,payment_order_id,org_id,kind,model,pool_bps,"
                  "eligible_revenue_cents,pool_cents,start_date,end_date,status,snapshot) values (gen_random_uuid(), "
                  ":c, :p, :o, 'RENEWAL', 'A_POOL', 500, 1, 0, '2026-10-01', '2026-10-31', 'ACTIVE', '{}'::jsonb)",
              c=ids["camp1"], p=ids["order1"], o=ids["org"])
        assert "uq_commission_campaigns_campaign_payment" in str(ei.value)

        # renovação paga (2º termo da campanha) ⇒ downgrade recusado, revisão mantida
        order3 = q(db, "insert into payment_orders (id,org_id,reference_type,reference_id,reference,items,amount_cents,"
                       "eligible_revenue_cents,services_cents,method,status,provider,instance_slug) values "
                       "(gen_random_uuid(), :o, 'campaign', 'REF-3', '{}'::jsonb, '[]'::jsonb, 50000, 50000, 0, "
                       "'pix', 'PAID', 'fake', 'sandbox') returning id", o=ids["org"])[0][0]
        renewal = q(db, "insert into commission_campaigns (id,campaign_id,payment_order_id,org_id,kind,model,pool_bps,"
                        "eligible_revenue_cents,pool_cents,start_date,end_date,status,snapshot) values "
                        "(gen_random_uuid(), :c, :p, :o, 'RENEWAL', 'A_POOL', 500, 50000, 2500, '2026-10-01', "
                        "'2026-10-31', 'ACTIVE', '{}'::jsonb) returning id",
                    c=ids["camp1"], p=order3, o=ids["org"])[0][0]
        with pytest.raises(Exception) as ei:
            alembic(db, "downgrade", "0005")
        assert "downgrade 0006 recusado" in str(ei.value)
        assert q(db, "select version_num from alembic_version")[0][0] == "0006"
        assert q(db, "select count(*) from commission_campaigns")[0][0] == 3

        # sem termos extras: volta ao formato 0005 e o upgrade seguinte reconverte
        q(db, "delete from commission_campaigns where id = :i", i=renewal)
        alembic(db, "downgrade", "0005")
        assert q(db, "select version_num from alembic_version")[0][0] == "0005"
        assert sorted(q(db, "select campaign_id, payment_order_id from commission_campaigns")) == sorted([
            (ids["camp1"], ids["order1"]), (ids["camp2"], ids["order2"])])
        assert q(db, "select campaign_id, amount_ucents from commission_accruals") == [(ids["camp1"], 777)]
        assert q(db, "select campaign_id, released_cents from commission_pool_releases") == [(ids["camp1"], 100)]
        # plano por tela reconstruído dos planos do dia congelados (perda documentada: dias não apropriados)
        assert q(db, "select plan, expected_total from commission_campaign_screens where screen_id = :s",
                 s=ids["s2"]) == [({"2026-09-01": 5}, 5)]
        assert q(db, "select count(*) from audit_logs where action = 'migration.0006.downgrade'")[0][0] == 1
        alembic(db, "upgrade", "head")
        assert q(db, "select count(*) from commission_campaigns where kind = 'INITIAL'")[0][0] == 2
        assert q(db, "select count(*) from commission_accruals where terms_id is not null")[0][0] == 1
        assert q(db, CHAIN_BAD_SQL)[0][0] == 0
