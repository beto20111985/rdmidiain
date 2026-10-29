"""Migrations 0004 (consolidação dos domínios), 0005 (seeds de sistema) e head — AÇÃO 002 / W-DB.

Head atual: 0009 (AÇÃO 003 — 0007 modo de aprovação/Admin Master, 0008 search_path seguro, 0009 selos; cenários em
``test_migrations_0007.py``/``_0008.py``/``_0009.py``; 0006 em ``test_migrations_0006.py``). Os testes abaixo usam ``head``.

Bancos DESCARTÁVEIS (rdtest_mig_*), dropados ao final. Cenários:
- head == create_all + DDL do harness (catálogo estrutural completo: colunas, defaults,
  índices, CHECK/UNIQUE/FK, triggers, corpo das funções, sequências, extensões) e drift 0;
- round-trip head → 0003 → head (estrutura da 0003 idêntica à de um banco recém-migrado);
- upgrade COM DADOS legados sintéticos equivalentes ao banco de dev (bitmask de organização,
  telas PAIRED/UNPAIRED, campanhas EN, dispositivo com token/segredo em claro, pareamento,
  lead/proposta/chamado/notificação no formato antigo) → conversões, auditoria encadeada,
  segredos ausentes; downgrade reverte o que é reversível;
- guarda: dados legados sem caminho automático abortam o upgrade (transação desfeita);
- seeds de sistema idempotentes (0005).
A cópia do banco de dev (pg_dump → pg_restore → upgrade) foi executada pelo W-DB (relatório).
"""
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from tests.migrations.test_migrations import ALEMBIC_INI, _url, alembic, q, scratch_db

LEGACY_TOKEN = "LEGACY-DEVICE-TOKEN-should-vanish-0004"
LEGACY_SECRET = "LEGACY-DEVICE-SECRET-should-vanish-0004"

# catálogo com corpo de funções/triggers e extensões (mais estrito que o do W1)
FULL_CATALOG_SQL = """
select 'col', table_name, column_name, data_type || ':' || coalesce(character_maximum_length::text,'') || ':' || is_nullable
       || ':' || coalesce(column_default,'') || ':' || coalesce(is_generated,'') || ':' || coalesce(generation_expression,'')
  from information_schema.columns where table_schema='public' and table_name <> 'alembic_version'
union all select 'idx', tablename, indexname, indexdef from pg_indexes where schemaname='public' and tablename <> 'alembic_version'
union all select 'con', conrelid::regclass::text, conname, pg_get_constraintdef(oid)
       from pg_constraint where connamespace='public'::regnamespace and conrelid::regclass::text <> 'alembic_version'
union all select 'trg', c.relname, t.tgname, pg_get_triggerdef(t.oid)
       from pg_trigger t join pg_class c on c.oid=t.tgrelid where not t.tgisinternal
union all select 'fn', p.proname, pg_get_function_identity_arguments(p.oid), md5(pg_get_functiondef(p.oid))
       from pg_proc p where p.pronamespace='public'::regnamespace and p.prokind='f'
union all select 'seq', sequence_name, data_type, '' from information_schema.sequences where sequence_schema='public'
union all select 'ext', extname, '', '' from pg_extension
order by 1,2,3,4
"""


def head_revision() -> str:
    return ScriptDirectory.from_config(Config(ALEMBIC_INI)).get_current_head()


def build_create_all_reference(db) -> None:
    """Mesmo schema do harness rápido (create_all + app.db.ddl + catálogo RBAC)."""
    import app.models  # noqa: F401
    from app.core.database import Base
    from app.core.rbac_sync import sync_catalog_sync
    from app.db import ddl

    eng = create_engine(_url(db))
    try:
        with eng.begin() as conn:
            for stmt in ddl.extension_statements() + ddl.base_functions():
                conn.execute(text(stmt))
            Base.metadata.create_all(conn)
            ddl.apply_all_sync(conn)
            sync_catalog_sync(conn)
    finally:
        eng.dispose()


def diff(a, b):
    sa_, sb = set(a), set(b)
    return sorted(sa_ - sb)[:20], sorted(sb - sa_)[:20]


def test_head_is_current_and_schema_equals_create_all_plus_ddl(_test_database):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import app.models  # noqa: F401
    from app.core.database import Base

    assert head_revision() == "0009"
    with scratch_db() as ref, scratch_db() as db:
        build_create_all_reference(ref)
        alembic(db, "upgrade", "head")
        assert q(db, "select version_num from alembic_version")[0][0] == head_revision()
        only_head, only_ref = diff(q(db, FULL_CATALOG_SQL), q(ref, FULL_CATALOG_SQL))
        assert only_head == [] and only_ref == [], (only_head, only_ref)
        eng = create_engine(_url(db))
        with eng.connect() as c:
            mc = MigrationContext.configure(c, opts={"compare_type": True, "compare_server_default": True})
            diffs = compare_metadata(mc, Base.metadata)
        eng.dispose()
        assert diffs == [], diffs[:5]


def test_round_trip_head_to_0003_and_back(_test_database):
    with scratch_db() as ref0003, scratch_db() as db:
        alembic(ref0003, "upgrade", "0003")
        alembic(db, "upgrade", "head")
        head_catalog = q(db, FULL_CATALOG_SQL)
        alembic(db, "downgrade", "0003")
        assert q(db, "select version_num from alembic_version")[0][0] == "0003"
        only_down, only_ref = diff(q(db, FULL_CATALOG_SQL), q(ref0003, FULL_CATALOG_SQL))
        assert only_down == [] and only_ref == [], (only_down, only_ref)
        alembic(db, "upgrade", "head")
        assert q(db, FULL_CATALOG_SQL) == head_catalog


def _insert_legacy_dev_like_data(db) -> dict:
    """Dados no formato da 0003 equivalentes aos do banco de dev (seed da AÇÃO 001)."""
    ids = {}
    ids["user"] = q(db, "insert into users (id,email,hashed_password,full_name,created_at,updated_at) values "
                        "(gen_random_uuid(),'vendedor@example.com','x','Vendedor',now(),now()) returning id")[0][0]
    for key, typ in (("org_rd", 3), ("org_est", 6), ("org_adv", 1)):
        ids[key] = q(db, "insert into organizations (id,name,document,document_type,type,is_active,created_at,updated_at) "
                         "values (gen_random_uuid(), :n, :d, 'CNPJ', :t, true, now(), now()) returning id",
                     n=f"Org {key}", d=f"DOC-{key}", t=typ)[0][0]
    ids["est"] = q(db, "insert into establishments (id,org_id,name,city,state,country,postal_code,timezone,is_active,"
                       "created_at,updated_at) values (gen_random_uuid(), :o, 'Academia', 'Goiânia', 'go', 'BRA', "
                       "'74.000-000', 'America/Sao_Paulo', true, now(), now()) returning id", o=ids["org_est"])[0][0]
    ids["screen_paired"] = q(db, "insert into screens (id,establishment_id,friendly_id,name,orientation,status,inches,"
                                 "estimated_audience,tv_owner,created_at,updated_at) values (gen_random_uuid(), :e, "
                                 "'TEL-GO-AAAA', 'Entrada', 'horizontal', 'PAIRED', 42.6, 350, 'ESTABLISHMENT', now(), "
                                 "now()) returning id", e=ids["est"])[0][0]
    ids["screen_unpaired"] = q(db, "insert into screens (id,establishment_id,friendly_id,name,orientation,status,"
                                   "tv_owner,created_at,updated_at) values (gen_random_uuid(), :e, 'TEL-GO-BBBB', "
                                   "'Caixa', 'vertical', 'UNPAIRED', 'RD', now(), now()) returning id", e=ids["est"])[0][0]
    ids["device"] = q(db, "insert into devices (id,screen_id,device_token,device_secret,platform,is_active,paired_at,"
                          "created_at) values (gen_random_uuid(), :s, :tok, :sec, 'ANDROID', true, now(), now()) "
                          "returning id", s=ids["screen_paired"], tok=LEGACY_TOKEN, sec=LEGACY_SECRET)[0][0]
    q(db, "update screens set device_id = :d where id = :s", d=ids["device"], s=ids["screen_paired"])
    q(db, "insert into pairing_requests (id,pairing_code,qr_token,expires_at,used_at,device_id,created_at) values "
          "(gen_random_uuid(),'717628','qr-legacy',now(),now(),:d,now())", d=ids["device"])
    for key, status, starts, ends, budget, reason in (
        ("camp_draft", "DRAFT", "2026-01-01 00:00:00+00", "2026-01-31 23:59:59+00", 500000, None),
        ("camp_approved", "APPROVED", None, None, 100000, None),
        ("camp_rejected", "REJECTED", None, None, 50000, "Conteudo inadequado"),
    ):
        ids[key] = q(db, "insert into campaigns (id,org_id,name,status,starts_at,ends_at,total_budget_cents,"
                         "rejected_reason,created_at,updated_at) values (gen_random_uuid(), :o, :n, :st, "
                         "CAST(:s AS timestamptz), CAST(:e AS timestamptz), :b, :r, now(), now()) returning id",
                     o=ids["org_rd"], n=key, st=status, s=starts, e=ends, b=budget, r=reason)[0][0]
    q(db, "insert into campaign_targets (id,campaign_id,target_type,target_value,is_exclude) values "
          "(gen_random_uuid(), :c, 'city', 'GO:goiania', false)", c=ids["camp_draft"])
    ids["lead"] = q(db, "insert into leads (id,name,status,assigned_to,source,created_at,updated_at) values "
                        "(gen_random_uuid(),'Lead','new',:u,NULL,now(),now()) returning id", u=ids["user"])[0][0]
    ids["proposal"] = q(db, "insert into proposals (id,title,status,total_cents,created_by,approved_by,created_at,"
                            "updated_at) values (gen_random_uuid(),'Proposta','SENT',30000,:u,:u,now(),now()) "
                            "returning id", u=ids["user"])[0][0]
    q(db, "insert into proposal_items (id,proposal_id,description,quantity,unit_price_cents,discount_percent) values "
          "(gen_random_uuid(), :p, 'Tela', 3, 10000, 5.0)", p=ids["proposal"])
    ids["ticket"] = q(db, "insert into tickets (id,title,status,priority,category,created_by,created_at,updated_at) "
                          "values (gen_random_uuid(),'Tela apagada','OPEN','high','suporte',:u,now(),now()) "
                          "returning id", u=ids["user"])[0][0]
    q(db, "insert into ticket_messages (id,ticket_id,content,author_id,is_internal,created_at) values "
          "(gen_random_uuid(), :t, 'mensagem legada', :u, false, now())", t=ids["ticket"], u=ids["user"])
    q(db, "insert into notifications (id,user_id,type,channel,title,idempotency_key,created_at) values "
          "(gen_random_uuid(), :u, 'legacy', 'in_app', 'x', 'k1', now())", u=ids["user"])
    q(db, "insert into notification_preferences (id,user_id,event_type,channel,is_enabled) values "
          "(gen_random_uuid(), :u, 'campaign.updated', 'email', true)", u=ids["user"])
    return ids


def test_upgrade_with_legacy_data_converts_audits_and_drops_secrets(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "0003")
        ids = _insert_legacy_dev_like_data(db)
        alembic(db, "upgrade", "head")

        assert q(db, "select id, business_roles from organizations order by name") == [
            (ids["org_adv"], ["ADVERTISER"]), (ids["org_est"], ["PARTNER"]), (ids["org_rd"], ["ADVERTISER", "PARTNER"])]
        assert q(db, "select state, city_key, region, postal_code from establishments") == [
            ("GO", "GO:goiania", "CENTRO_OESTE", "74000000")]
        assert q(db, "select friendly_id, status, orientation, inches, estimated_daily_audience, audience_method, "
                     "sellable, device_id from screens order by friendly_id") == [
            ("TEL-GO-AAAA", "ACTIVE", "HORIZONTAL", 43, 350, "MANUAL", True, None),
            ("TEL-GO-BBBB", "ACTIVE", "VERTICAL", None, None, None, True, None)]
        # dispositivo legado: revogado, sem chave, segredos fora do banco
        dev = q(db, "select status, platform, public_key, key_alg, revoked_at is not null, screen_id from devices")
        assert dev == [("REVOKED", "android", "", "LEGACY", True, ids["screen_paired"])]
        cols = {r[0] for r in q(db, "select column_name from information_schema.columns where table_name='devices'")}
        assert not cols & {"device_token", "device_secret", "is_active", "ip_address", "ssid", "cycle_anchor"}
        dump = q(db, "select coalesce(string_agg(before_data::text || coalesce(after_data::text,''), ' '), '') "
                     "from audit_logs")[0][0]
        assert LEGACY_TOKEN not in dump and LEGACY_SECRET not in dump
        assert q(db, "select count(*) from pairing_requests")[0][0] == 0
        # campanhas EN → PT, datas no fuso de São Paulo, histórico de status
        camps = {r[0]: r[1:] for r in q(db, "select name, status, start_date::text, end_date::text, rejection_reason, "
                                            "requires_new_payment from campaigns")}
        assert camps["camp_draft"] == ("RASCUNHO", "2025-12-31", "2026-01-31", None, False)
        assert camps["camp_approved"] == ("RASCUNHO", None, None, None, False)
        assert camps["camp_rejected"] == ("REJEITADA", None, None, "Conteudo inadequado", True)
        assert sorted(q(db, "select from_status, to_status from campaign_status_history")) == [
            ("APPROVED", "RASCUNHO"), ("DRAFT", "RASCUNHO"), ("REJECTED", "REJEITADA")]
        assert q(db, "select target_type from campaign_targets") == [("CITY",)]
        # orçamento legado sem coluna equivalente: preservado só na auditoria da migração
        budget = q(db, "select before_data from audit_logs where action = 'migration.0004.campaign' "
                       "and entity_id = :c", c=str(ids["camp_draft"]))[0][0]
        assert budget["total_budget_cents"] == 500000 and budget["status"] == "DRAFT"
        # CRM / suporte / notificações
        assert q(db, "select stage, owner_id, source from leads") == [("NOVO", ids["user"], "manual")]
        assert q(db, "select number ~ '^PROP-[0-9]{4}-[ABCDEFGHJMNPQRSTUVYZ2-9]{6}$', seller_id, discount_approved_by, status, subtotal_cents, "
                     "total_cents from proposals") == [(True, ids["user"], ids["user"], "ENVIADA", 30000, 30000)]
        assert q(db, "select position, line_total_cents, details from proposal_items") == [
            (0, 30000, {"legacy_discount_percent": 5.0})]
        assert q(db, "select number ~ '^CH-[ABCDEFGHJMNPQRSTUVYZ2-9]{6}$', status, priority, category from tickets") == [
            (True, "ABERTO", "ALTA", "OUTRO")]
        assert q(db, "select body from ticket_messages") == [("mensagem legada",)]
        assert q(db, "select count(*) from notifications")[0][0] == 0
        assert q(db, "select channel from notification_preferences") == [("EMAIL",)]
        # auditoria da migração encadeada e íntegra
        actions = dict(q(db, "select action, count(*) from audit_logs where action like 'migration.0004.%' group by 1"))
        assert actions["migration.0004.organization_business_roles"] == 3
        assert actions["migration.0004.campaign"] == 3
        assert actions["migration.0004.device_revoked"] == 1
        bad = q(db, "with x as (select a.seq, a.prev_hash, a.row_hash, lag(a.row_hash) over (order by a.seq) p, "
                    "encode(sha256(convert_to(rd_audit_hash_material(a, a.prev_hash), 'UTF8')), 'hex') c "
                    "from audit_logs a) select count(*) filter (where c <> row_hash or prev_hash <> "
                    "coalesce(p, repeat('0', 64))) from x")[0][0]
        assert bad == 0
        # código público da tela continua imutável após a migração (trigger)
        with pytest.raises(Exception) as ei:
            q(db, "update screens set friendly_id = 'TEL-GO-ZZZZ' where friendly_id = 'TEL-GO-AAAA'")
        assert "RD_IMMUTABLE_COLUMN" in str(ei.value)

        # dado criado no formato novo (categoria da taxonomia) não pode impedir o rollback
        q(db, "update establishments set category_id = (select id from taxonomy_nodes where kind = 'CATEGORY' "
              "order by slug limit 1)")
        # downgrade: reversível no que tem equivalente; perdas documentadas
        alembic(db, "downgrade", "0003")
        assert q(db, "select category_id from establishments") == [(None,)]  # taxonomia nova não existe na 0003
        assert sorted(q(db, "select name, type from organizations")) == [
            ("Org org_adv", 1), ("Org org_est", 2), ("Org org_rd", 3)]
        assert sorted(q(db, "select name, status from campaigns")) == [
            ("camp_approved", "DRAFT"), ("camp_draft", "DRAFT"), ("camp_rejected", "REJECTED")]
        assert q(db, "select is_active, platform from devices") == [(False, "ANDROID")]
        assert q(db, "select content from ticket_messages") == [("mensagem legada",)]
        assert q(db, "select status, assigned_to from leads") == [("NEW", ids["user"])]
        alembic(db, "upgrade", "head")
        assert q(db, "select count(*) from devices where status = 'REVOKED'")[0][0] == 1


@pytest.mark.parametrize("table_sql", [
    "insert into ledger_entries (id,type,org_id,amount_cents,idempotency_key,created_at) values "
    "(gen_random_uuid(),'credit',(select id from organizations limit 1),100,'k',now())",
    "insert into creatives (id,org_id,creative_id,name,media_file_id,duration_seconds,status,created_at,updated_at) "
    "values (gen_random_uuid(),(select id from organizations limit 1),'RD-AAAAAA','c',"
    "(select id from media_files limit 1),45,'DRAFT',now(),now())",
])
def test_upgrade_refuses_unmigratable_legacy_rows_0004(_test_database, table_sql):
    with scratch_db() as db:
        alembic(db, "upgrade", "0003")
        q(db, "insert into organizations (id,name,document,document_type,type,is_active,created_at,updated_at) "
              "values (gen_random_uuid(),'O','DOC','CNPJ',1,true,now(),now())")
        if "creatives" in table_sql:
            q(db, "insert into users (id,email,hashed_password,full_name,created_at,updated_at) values "
                  "(gen_random_uuid(),'u@example.com','x','U',now(),now())")
            q(db, "insert into media_files (id,org_id,filename,original_filename,content_type,mime_type,size_bytes,"
                  "storage_path,storage_provider,status,created_by,created_at,updated_at) values (gen_random_uuid(),"
                  "(select id from organizations limit 1),'f','f.png','image','image/png',1,'p','local','READY',"
                  "(select id from users limit 1),now(),now())")
        q(db, table_sql)
        with pytest.raises(Exception) as ei:
            alembic(db, "upgrade", "head")
        msg = str(ei.value)
        assert "0004" in msg
        assert ("ledger_entries" in msg) if "ledger" in table_sql else ("media_files" in msg and "30/60/120" in msg)
        assert q(db, "select version_num from alembic_version")[0][0] == "0003"  # transação desfeita


def test_system_seeds_are_idempotent(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "head")
        snap = "select (select count(*) from content_policies), (select count(*) from segments), " \
               "(select count(*) from taxonomy_nodes), (select count(*) from rotation_policies)"
        first = q(db, snap)
        assert first == [(7, 15, 16, 1)]
        assert dict(q(db, "select s.slug, p.effect from content_policies p join segments s on s.id = p.segment_id")) == {
            "apostas": "BLOCK", "conteudo-adulto": "BLOCK", "armas": "BLOCK", "bebidas-alcoolicas": "ALLOW",
            "tabaco": "ALLOW", "politica": "ALLOW", "farmaceuticos": "ALLOW"}
        assert q(db, "select internal_seconds, external_seconds, effective_to from rotation_policies "
                     "where scope = 'GLOBAL'") == [(120, 120, None)]
        alembic(db, "downgrade", "0004")
        alembic(db, "upgrade", "head")
        assert q(db, snap) == first


def test_runtime_role_cannot_mutate_append_only_tables(_test_database):
    """Se o papel de runtime existe no cluster, a 0004 tira UPDATE/DELETE/TRUNCATE dele nas
    tabelas append-only (camada extra além dos triggers) e DELETE/TRUNCATE nas financeiras com guarda.
    AÇÃO 003 (0007): nas trilhas seladas (``ddl.DEFINER_ONLY_TABLES``) nem INSERT — só função definer."""
    with scratch_db() as db:
        alembic(db, "upgrade", "head")
        if not q(db, "select exists (select 1 from pg_roles where rolname = 'rd_app')")[0][0]:
            pytest.skip("papel rd_app inexistente neste cluster (criado pelo db-init)")
        from app.db import ddl

        for t in ddl.APPEND_ONLY_TABLES:
            row = q(db, "select has_table_privilege('rd_app', :t, 'INSERT'), has_table_privilege('rd_app', :t, "
                        "'UPDATE'), has_table_privilege('rd_app', :t, 'DELETE'), has_table_privilege('rd_app', :t, "
                        "'TRUNCATE')", t=t)[0]
            expected = (t not in ddl.DEFINER_ONLY_TABLES, False, False, False)
            assert row == expected, (t, row)
        for t in ("webhook_events", "financial_approval_requests", "withdrawals"):
            row = q(db, "select has_table_privilege('rd_app', :t, 'UPDATE'), has_table_privilege('rd_app', :t, "
                        "'DELETE')", t=t)[0]
            assert row == (True, False), (t, row)

