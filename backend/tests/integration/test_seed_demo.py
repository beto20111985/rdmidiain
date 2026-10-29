"""CLI ``seed-demo`` (AÇÃO 002 / W-DB): dados de DEMONSTRAÇÃO ponta a ponta pelos serviços reais.

PostgreSQL/Redis reais (harness), pipeline de mídia REAL (Pillow/ffmpeg/ffprobe), checkout →
webhook ASSINADO do provedor FALSO → aprovação → manifesto assinado; conflito de bebida na
distribuidora; idempotência; ``--purge`` (fecho por FK, sem órfãos, auditoria íntegra).
"""
import re

import pytest
from sqlalchemy import select, text

from app.core.config import settings
from app.core.storage import LocalStorageProvider, set_storage
from tests.fin_helpers import fin_env  # noqa: F401  (fixture: provedor FALSO + despacho capturado)

DEMO_ORGS_SQL = "select count(*) from organizations where name like 'DEMONSTRAÇÃO —%'"


async def _scalar(db, sql, **p):
    return (await db.execute(text(sql), p)).scalar()


async def test_seed_demo_refused_outside_development(monkeypatch):
    from app import cli_demo

    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    with pytest.raises(SystemExit) as e1:
        await cli_demo.cmd_seed_demo()
    with pytest.raises(SystemExit) as e2:
        await cli_demo.cmd_seed_demo_purge()
    assert "development" in str(e1.value) and "development" in str(e2.value)


async def test_seed_demo_full_cycle_idempotent_and_purge(db, fin_env, tmp_path, monkeypatch, capsys):  # noqa: F811
    from app import cli_demo
    from app.core.audit import verify_chain
    from app.models.campaigns import Campaign, Creative
    from app.modules.conflicts import service as CS
    from app.modules.inventory import service as inv

    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    set_storage(LocalStorageProvider(str(tmp_path / "storage")))
    try:
        await cli_demo.cmd_seed_demo()
        out = capsys.readouterr().out
        assert "BLOCK (SEGMENTO)" in out and "senha" in out
        assert not re.search(r"(?i)password\s*[:=]", out)  # nenhuma senha impressa

        db.expire_all()
        statuses = sorted(r[0] for r in (await db.execute(
            select(Campaign.status).where(Campaign.name.like("DEMONSTRAÇÃO —%")))).all())
        assert statuses == ["ATIVA", "EM_ANALISE", "RASCUNHO", "RASCUNHO"]
        assert await _scalar(db, DEMO_ORGS_SQL) == 8
        tz = dict((await db.execute(text(
            "select city, timezone from establishments where name like 'DEMONSTRAÇÃO —%'"))).all())
        assert tz == {"Goiânia": "America/Sao_Paulo", "São Paulo": "America/Sao_Paulo",
                      "Manaus": "America/Manaus", "Rio Branco": "America/Rio_Branco"}
        screens = (await db.execute(text(
            "select s.friendly_id, s.sellable from screens s join establishments e on e.id = s.establishment_id "
            "where e.name like 'DEMONSTRAÇÃO —%'"))).all()
        assert len(screens) == 9 and all(sellable for _, sellable in screens)
        assert all(re.fullmatch(r"TEL-(GO|SP|AM|AC)-[A-Z0-9]{4}", fid) for fid, _ in screens)
        media = (await db.execute(text(
            "select kind, status, duration_ms, width, height from media_files where name like 'DEMONSTRAÇÃO —%'"))).all()
        assert len(media) == 6 and all(m[1] == "READY" for m in media)
        video = [m for m in media if m[0] == "VIDEO"]
        assert len(video) == 1 and 29000 <= video[0][2] <= 30040 and (video[0][3], video[0][4]) == (1920, 1080)
        assert {(m[3], m[4]) for m in media} == {(1920, 1080), (1080, 1920)}
        # contrato: aceite manual rotulado como demonstração; estabelecimentos ativos
        assert await _scalar(db, "select count(*) from contract_acceptances where method = 'MANUAL_RECORD' "
                                 "and signer_name like 'DEMONSTRAÇÃO%'") == 4
        # pagamento simulado pelo caminho de desenvolvimento (webhook assinado) e ack simulado AUDITADOS
        assert await _scalar(db, "select count(*) from audit_logs where action = 'dev.webhook_simulated'") == 2
        assert await _scalar(db, "select count(*) from audit_logs where action = 'seed.demo.player_ack_simulated'") >= 1
        assert await _scalar(db, "select count(*) from payment_orders where status = 'PAID'") == 2
        # usuários sem senha conhecida
        assert await _scalar(db, "select count(*) from users where email like 'demonstracao.%' "
                                 "and not must_change_password") == 0
        # conflito: bebida × distribuidora de bebidas = BLOCK SEGMENTO
        bev = (await db.execute(select(Creative).where(Creative.name.like("%refrigerante%")))).scalar_one()
        dist = await _scalar(db, "select id from establishments where name like '%Distribuidora%'")
        d = await CS.evaluate_conflict(db, bev.id, dist, record=False)
        assert (d.result, d.reason_code) == ("BLOCK", "SEGMENTO")
        # manifesto de uma tela demo contém o criativo da campanha ATIVA
        academia = (await db.execute(select(Creative).where(Creative.name.like("%Academia%")))).scalar_one()
        sid = await _scalar(db, "select s.id from screens s join establishments e on e.id = s.establishment_id "
                                "where e.city = 'Manaus' and e.name like 'DEMONSTRAÇÃO —%' order by s.name limit 1")
        sm = await inv.current_manifest(db, sid)
        await db.commit()
        assert academia.code in {c.get("code") for c in sm.manifest["creatives"].values()}
        assert sm.manifest["timezone"] == "America/Manaus"

        # idempotente: nada duplica
        snap = "select (select count(*) from organizations), (select count(*) from screens), " \
               "(select count(*) from campaigns), (select count(*) from media_files), (select count(*) from orders)"
        before = (await db.execute(text(snap))).one()
        await cli_demo.cmd_seed_demo()
        db.expire_all()
        assert (await db.execute(text(snap))).one() == before

        # purge: demo removida, sem órfãos (verificado dentro da transação), auditoria íntegra
        await cli_demo.cmd_seed_demo_purge()
        out = capsys.readouterr().out
        assert "removido" in out
        db.expire_all()
        assert await _scalar(db, DEMO_ORGS_SQL) == 0
        assert await _scalar(db, "select count(*) from campaigns") == 0
        assert await _scalar(db, "select count(*) from ledger_transactions") == 0
        assert await _scalar(db, "select count(*) from users where email like 'demonstracao.%' and is_active") == 0
        assert await _scalar(db, "select count(*) from role_assignments ra join users u on u.id = ra.user_id "
                                 "where u.email like 'demonstracao.%'") == 0
        assert (await verify_chain(db)).ok
        assert await _scalar(db, "select count(*) from audit_logs where action = 'seed.demo.purged'") == 1
    finally:
        set_storage(None)
