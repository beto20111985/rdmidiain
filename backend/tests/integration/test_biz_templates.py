"""W-BIZ — Editor de templates: spec segura validada, renderização NO SERVIDOR em PNG/JPG
1920×1080 e 1080×1920 (Pillow abre e confere dimensões e pixels), variáveis, assets de
imagem, entrega como mídia (W-MEDIA) e geração de versão de arte do designer."""
from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image

from app.core.storage import LocalStorageProvider, set_storage
from tests.factories import login_as, make_org, make_user


@pytest.fixture(autouse=True)
async def _drain_after_test(clean_state):
    yield
    from app.modules.notifications.service import drain

    await drain()


@pytest.fixture
def storage(tmp_path):
    set_storage(LocalStorageProvider(tmp_path / "storage"))
    yield
    set_storage(None)


def _spec(asset_id=None, text="Promoção {{produto}} por {{preco}}"):
    layers = [
        {"type": "rect", "box": {"col": 0, "row": 18, "col_span": 24, "row_span": 6}, "color": "#0B84FF", "opacity": 100},
        {"type": "text", "box": {"col": 1, "row": 2, "col_span": 22, "row_span": 8}, "text": text,
         "font": "dejavu-sans-bold", "size_px": 140, "color": "#FFFFFF", "align": "center", "valign": "middle",
         "max_lines": 2},
    ]
    if asset_id:
        layers.append({"type": "image", "box": {"col": 18, "row": 0, "col_span": 6, "row_span": 6},
                       "asset_id": asset_id, "fit": "contain"})
    return {"background": {"color": "#05070A"},
            "variables": [{"name": "produto", "label": "Produto", "max_len": 40},
                          {"name": "preco", "label": "Preço", "max_len": 20}],
            "layers": layers}


async def _designer(client, db):
    u = await make_user(db, role="designer")  # templates.manage + design.work GLOBAL
    return await login_as(client, u)


def _png(w, h, color):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


@pytest.mark.parametrize("orientation,size", [("HORIZONTAL", (1920, 1080)), ("VERTICAL", (1080, 1920))])
async def test_render_png_and_jpeg_with_exact_dimensions(client, db, storage, mail, orientation, size):
    s = await _designer(client, db)
    a = await client.post("/api/v1/templates/assets", headers=s.headers,
                          files={"file": ("logo.png", _png(300, 300, (255, 0, 0)), "image/png")})
    assert a.status_code == 201, a.text
    r = await client.post("/api/v1/templates", headers=s.headers, json={
        "name": f"Oferta {orientation}", "orientation": orientation, "spec": _spec(a.json()["id"]), "status": "ATIVO"})
    assert r.status_code == 201, r.text
    t = r.json()
    assert (t["width"], t["height"]) == size
    for fmt, pil_fmt in (("PNG", "PNG"), ("JPEG", "JPEG")):
        p = await client.post(f"/api/v1/templates/{t['id']}/preview", headers=s.headers,
                              json={"variables": {"produto": "Café", "preco": "R$ 9,90"}, "format": fmt})
        assert p.status_code == 200, p.text
        with Image.open(io.BytesIO(p.content)) as im:
            assert im.format == pil_fmt and im.size == size
            rgb = im.convert("RGB")
            w, h = size
            # fundo, faixa azul (rect) e logotipo vermelho (image) na posição da grade
            assert _close(rgb.getpixel((5, 5)), (5, 7, 10))
            assert _close(rgb.getpixel((w // 2, h - 5)), (11, 132, 255))
            assert _close(rgb.getpixel((int(w * 21 / 24), int(h * 3 / 24))), (255, 0, 0))
            # texto foi desenhado (pixels brancos dentro da caixa do texto)
            box = rgb.crop((int(w / 24), int(h * 2 / 24), int(w * 23 / 24), int(h * 10 / 24)))
            assert any(px[0] > 230 and px[1] > 230 and px[2] > 230 for px in box.getdata())


def _close(a, b, tol=12):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


async def test_spec_validation_is_strict(client, db, storage, mail):
    s = await _designer(client, db)
    bad_specs = [
        {"background": {"color": "red"}, "layers": []},  # cor fora do padrão
        {"background": {"color": "#000000"}, "layers": [{"type": "html", "box": {"col": 0, "row": 0, "col_span": 1,
                                                                                    "row_span": 1}, "html": "<script>"}]},
        {"background": {"color": "#000000"}, "layers": [{"type": "text", "box": {"col": 20, "row": 0, "col_span": 10,
                                                                                    "row_span": 2}, "text": "x"}]},
        {"background": {"color": "#000000"}, "layers": [{"type": "text", "box": {"col": 0, "row": 0, "col_span": 4,
                                                                                    "row_span": 2}, "text": "x",
                                                         "font": "../../etc/passwd"}]},
        {"background": {"color": "#000000"}, "layers": [{"type": "text", "box": {"col": 0, "row": 0, "col_span": 4,
                                                                                    "row_span": 2}, "text": "{{nao_declarada}}"}]},
        {"background": {"color": "#000000"}, "layers": [], "script": "alert(1)"},  # campo desconhecido
        {"background": {"color": "#000000"}, "layers": [{"type": "image", "box": {"col": 0, "row": 0, "col_span": 4,
                                                                                     "row_span": 2},
                                                         "asset_id": str(uuid.uuid4())}]},
    ]
    for spec in bad_specs:
        r = await client.post("/api/v1/templates", headers=s.headers,
                              json={"name": "Inválido", "orientation": "HORIZONTAL", "spec": spec})
        assert r.status_code == 400, (spec, r.text)
    # texto com "HTML" é só texto desenhado; variável longa demais é recusada
    r = await client.post("/api/v1/templates", headers=s.headers, json={
        "name": "Texto", "orientation": "HORIZONTAL", "spec": _spec(text="<b>{{produto}}</b> {{preco}}")})
    assert r.status_code == 201
    p = await client.post(f"/api/v1/templates/{r.json()['id']}/preview", headers=s.headers,
                          json={"variables": {"produto": "x" * 41}})
    assert p.status_code == 400 and p.json()["error"]["code"] == "TEMPLATE_VARIABLE_TOO_LONG"
    p = await client.post(f"/api/v1/templates/{r.json()['id']}/preview", headers=s.headers,
                          json={"variables": {"hacker": "1"}})
    assert p.status_code == 400
    # anunciante não usa o editor
    org = await make_org(db)
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    sa = await login_as(client, adv)
    assert (await client.get("/api/v1/templates", headers=sa.headers)).status_code == 403


async def test_render_delivers_media_and_designer_version(client, db, storage, mail):
    s = await _designer(client, db)
    org = await make_org(db)
    t = (await client.post("/api/v1/templates", headers=s.headers, json={
        "name": "Cardápio", "orientation": "VERTICAL", "spec": _spec(), "status": "ATIVO"})).json()
    r = await client.post(f"/api/v1/templates/{t['id']}/render", headers=s.headers, json={
        "variables": {"produto": "Prato do dia", "preco": "R$ 25,00"}, "format": "PNG", "org_id": str(org.id),
        "deliver_as_media": True, "name": "Cardápio do almoço"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["media_id"] and (body["width"], body["height"]) == (1080, 1920)
    from app.models.media import MediaFile

    m = await db.get(MediaFile, uuid.UUID(body["media_id"]))
    assert m is not None and m.org_id == org.id
    f = await client.get(f"/api/v1/templates/renders/{body['id']}/file", headers=s.headers)
    with Image.open(io.BytesIO(f.content)) as im:
        assert im.size == (1080, 1920)

    # versão da arte do designer gerada pelo template
    client_user = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    sc = await login_as(client, client_user)
    req = (await client.post("/api/v1/design/requests", headers=sc.headers, json={
        "org_id": str(org.id), "title": "Cardápio", "briefing": "Usar template do cardápio",
        "orientation": "VERTICAL"})).json()
    await client.post(f"/api/v1/design/requests/{req['id']}/quote", headers=s.headers,
                      json={"billing": "ISENTO", "price_cents": 0})
    v = await client.post(f"/api/v1/design/requests/{req['id']}/versions/from-template", headers=s.headers, json={
        "template_id": t["id"], "variables": {"produto": "Feijoada", "preco": "R$ 30,00"}})
    assert v.status_code == 201, v.text
    assert v.json()["source"] == "TEMPLATE" and (v.json()["width"], v.json()["height"]) == (1080, 1920)
    # orientação errada é recusada
    h = (await client.post("/api/v1/templates", headers=s.headers, json={
        "name": "Horizontal", "orientation": "HORIZONTAL", "spec": _spec()})).json()
    bad = await client.post(f"/api/v1/design/requests/{req['id']}/versions/from-template", headers=s.headers,
                            json={"template_id": h["id"], "variables": {}})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "TEMPLATE_ORIENTATION_MISMATCH"
