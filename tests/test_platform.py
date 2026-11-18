"""Вход по коду, тарифы и лимиты, документы и готовность к заявке, миграция колонок."""
import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import config, db, runtime
from app.api.auth import make_link_token
from app.db import Document, User, utcnow
from app.services import billing, documents as docsvc, login


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.configure(f"sqlite+aiosqlite:///{tmp_path}/p.db")
    monkeypatch.setattr(runtime, "bot_username", "kyran_test_bot")
    from app.main import app

    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer " + make_link_token(2002)
        import time

        for _ in range(100):  # дождаться первого сбора, чтобы фоновая задача не писала в базу во время теста
            if c.get("/api/search?q=ремонт").json()["items"]:
                break
            time.sleep(0.1)
        time.sleep(0.3)
        yield c


def test_login_by_code_and_logout_all(client):
    anon = {"Authorization": ""}
    d = client.post("/api/auth/start", headers=anon).json()
    assert len(d["code"]) == 6 and d["link"].endswith("login_" + d["nonce"])
    assert client.get("/api/auth/poll", params={"nonce": d["nonce"]}, headers=anon).json() == {"token": None}

    async def confirm():
        async with db.Session() as s:
            from app.services import core

            await core.get_or_create_user(s, 3003, "Тест")
            assert await login.bind(s, 3003, code=d["code"])
            assert await login.bind(s, 3003, code=d["code"]) is False  # повторно нельзя
    asyncio.run(confirm())

    token = client.get("/api/auth/poll", params={"nonce": d["nonce"]}, headers=anon).json()["token"]
    assert token
    # токен выдаётся один раз
    assert client.get("/api/auth/poll", params={"nonce": d["nonce"]}, headers=anon).status_code == 410

    h = {"Authorization": "Bearer " + token}
    assert client.get("/api/me", headers=h).json()["user"]["id"] == 3003
    assert client.post("/api/auth/logout-all", headers=h).json() == {"ok": True}
    assert client.get("/api/me", headers=h).status_code == 401


def test_plan_trial_and_grant():
    u = User(id=1, created_at=utcnow(), plan="trial", token_version=0)
    plan, active, until = billing.current_plan(u)
    assert plan.code == "trial" and active and (until - utcnow()).days in (13, 14)
    u.created_at = utcnow() - timedelta(days=20)
    assert billing.current_plan(u)[1] is False
    billing.grant(u, "pro", 1)
    plan, active, until = billing.current_plan(u)
    assert plan.code == "pro" and active and (until - utcnow()).days >= 29
    billing.grant(u, "pro", 1)  # продление суммируется
    assert (billing.current_plan(u)[2] - utcnow()).days >= 59
    with pytest.raises(ValueError):
        billing.grant(u, "gold", 1)


def test_documents_upload_and_readiness(client):
    r = client.post("/api/documents", data={"title": "Справка об отсутствии налоговой задолженности", "kind": "tax",
                                            "expires_at": (utcnow() + timedelta(days=10)).date().isoformat()},
                    files={"file": ("spravka.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["status"] == "expiring" and doc["size"] == 13
    assert client.get(f"/api/documents/{doc['id']}/file").content == b"%PDF-1.4 test"
    assert client.post("/api/documents", data={"title": "x"}, files={"file": ("a.exe", b"MZ", "application/x-msdownload")}).status_code == 400

    client.post("/api/documents", data={"title": "Лицензия на строительно-монтажные работы", "kind": "license"})
    items = client.get("/api/documents").json()["items"]
    assert len(items) == 2

    async def check():
        async with db.Session() as s:
            res = await docsvc.readiness(s, 2002, [
                "Справка налогового органа об отсутствии налоговой задолженности",
                "Государственная лицензия на СМР",
                "Сертификат ISO 9001",
            ], utcnow() + timedelta(days=5))
        return res
    res = asyncio.run(check())
    assert [x["status"] for x in res["items"]] == ["ready", "ready", "missing"]
    assert res["ready"] == 2 and res["total"] == 3
    # на дату позже окончания справки — уже просрочена
    async def later():
        async with db.Session() as s:
            return await docsvc.readiness(s, 2002, ["Справка об отсутствии налоговой задолженности"], utcnow() + timedelta(days=20))
    assert asyncio.run(later())["items"][0]["status"] == "expired"

    assert client.delete(f"/api/documents/{doc['id']}").json() == {"ok": True}
    assert len(client.get("/api/documents").json()["items"]) == 1


def test_document_limit(client, monkeypatch):
    monkeypatch.setitem(billing.PLANS, "trial", billing.Plan("trial", "Пробный", 0, 1, 1, 1, ()))
    assert client.post("/api/documents", data={"title": "Первый"}).status_code == 200
    r = client.post("/api/documents", data={"title": "Второй"})
    assert r.status_code == 402 and "лимит" in r.json()["detail"]


def test_upgrade_request_and_me_plan(client):
    me = client.get("/api/me").json()
    assert me["plan"]["code"] == "trial" and me["plan"]["active"] and len(me["plan"]["catalog"]) == 3
    assert me["bot"] == "kyran_test_bot"
    r = client.post("/api/billing/request", json={"plan": "pro", "months": 3, "contact": "+77001112233"})
    assert r.status_code == 200 and r.json()["id"]
    assert client.post("/api/billing/request", json={"plan": "trial"}).status_code == 400


def test_migration_adds_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(200))")
    con.execute("INSERT INTO users (id, name) VALUES (1, 'Старый')")
    con.commit()
    con.close()
    db.configure(f"sqlite+aiosqlite:///{path}")
    asyncio.run(db.init_db())
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(users)")}
    assert {"plan", "plan_until", "token_version", "company_bin", "keywords"} <= cols
    row = sqlite3.connect(path).execute("SELECT plan, token_version FROM users WHERE id=1").fetchone()
    assert row == ("trial", 0)


def test_market_insights_and_ask(client):
    import time

    for _ in range(50):
        items = client.get("/api/search?q=кровли").json()["items"]
        if items:
            break
        time.sleep(0.1)
    lot_id = items[0]["id"]
    r = client.get(f"/api/lots/{lot_id}/insights").json()
    assert r["available"] and r["similar_count"] >= 5
    p = r["price"]
    assert 0 < p["median_discount"] < 50 and p["range"][0] <= p["recommended"] <= p["range"][1]
    assert r["competitors"] and r["customer"]["total"] >= 3 and sum(r["histogram"]["counts"]) > 0
    # без ключа ИИ — понятный отказ
    a = client.post(f"/api/lots/{lot_id}/ask", json={"question": "Есть ли аванс?"})
    assert a.status_code == 503 and "ANTHROPIC_API_KEY" in a.json()["detail"]


def test_landing_and_public_plans(client):
    html = client.get("/", headers={"Authorization": ""}).text
    assert "Kyran" in html and "/api/plans" in html
    plans = client.get("/api/plans", headers={"Authorization": ""}).json()
    assert [p["code"] for p in plans["plans"]] == ["start", "pro", "team"] and plans["bot"] == "kyran_test_bot"


def test_expired_plan_blocks_analysis(client, tmp_path):
    import sqlite3

    client.get("/api/me")  # создаёт пользователя
    con = sqlite3.connect(tmp_path / "p.db", timeout=10)
    con.execute("UPDATE users SET plan='start', plan_until='2020-01-01 00:00:00.000000' WHERE id=2002")
    con.commit()
    con.close()
    me = client.get("/api/me").json()
    assert me["plan"]["active"] is False
    lot_id = client.get("/api/search?q=ремонт").json()["items"][0]["id"]
    r = client.post(f"/api/lots/{lot_id}/analyze")
    assert r.status_code == 402 and "истёк" in r.json()["detail"]


def test_generate_application_documents(client, monkeypatch):
    import io

    from docx import Document as Docx

    from app.ai import client as ai

    client.put("/api/me", json={"keywords": ["ремонт кровли"], "company_name": "ТОО «Тест Кровля»", "company_bin": "123456789012",
                                "director": "Ахметов А.А.", "phone": "+7 700 000 00 00"})
    lot_id = client.get("/api/search?q=кровли").json()["items"][0]["id"]
    info = client.get(f"/api/lots/{lot_id}/docs").json()
    assert [k["code"] for k in info["kinds"]] == ["tech", "clarify", "cover", "inventory", "experience"]
    assert "адрес" in info["missing"] and "БИН" not in info["missing"]

    # Без ИИ: письмо, опись, справка — готовы сразу; техпредложение — понятный отказ
    for kind in ("cover", "inventory", "experience"):
        r = client.post(f"/api/lots/{lot_id}/docs/{kind}")
        assert r.status_code == 200, r.text
        g = r.json()
        assert g["by_ai"] is False and g["filename"].endswith(".docx")
        f = client.get(f"/api/generated/{g['id']}/file")
        text = "\n".join(p.text for p in Docx(io.BytesIO(f.content)).paragraphs)
        assert "ТОО «Тест Кровля»" in text
    assert client.post(f"/api/lots/{lot_id}/docs/tech").status_code == 400
    assert client.post(f"/api/lots/{lot_id}/docs/clarify").status_code == 400  # нет разбора и нет ИИ

    # С ИИ (замокан)
    monkeypatch.setattr(ai, "available", lambda: True)

    async def fake(model, system, prompt, max_tokens=4000):
        if "questions" in prompt and "scope" not in prompt:
            return {"questions": ["Уточните площадь кровли", "Допускается ли аналог материала?"]}
        return {"intro": "Поставщик предлагает выполнить текущий ремонт кровли.",
                "scope": [{"name": "Демонтаж покрытия", "qty": "1450", "unit": "м²", "note": ""}],
                "method": ["Демонтаж", "Монтаж"], "timeline": "[уточнить]", "resources": [], "quality": ["Акты скрытых работ"],
                "warranty": "24 месяца", "compliance": [{"requirement": "Лицензия СМР", "response": "Имеется"}]}

    monkeypatch.setattr(ai, "ask_json", fake)
    t = client.post(f"/api/lots/{lot_id}/docs/tech").json()
    assert t["by_ai"] is True
    body = Docx(io.BytesIO(client.get(f"/api/generated/{t['id']}/file").content))
    alltext = "\n".join(p.text for p in body.paragraphs) + "\n".join(c.text for tb in body.tables for r in tb.rows for c in r.cells)
    assert "Демонтаж покрытия" in alltext and "24 месяца" in alltext
    c = client.post(f"/api/lots/{lot_id}/docs/clarify").json()
    assert c["by_ai"] is True
    me = client.get("/api/me").json()
    assert me["plan"]["usage"]["generations"] == 2
    latest = {k["code"]: k["doc"] for k in client.get(f"/api/lots/{lot_id}/docs").json()["kinds"]}
    assert all(latest.values())


def test_weekly_report(client):
    client.put("/api/me", json={"keywords": ["ремонт кровли", "канцтовары", "мебель", "ремонт"]})
    r = client.get("/api/report/weekly").json()
    assert r["missed"]["count"] > 0 and r["missed"]["total"] > 0 and r["winners"]
    assert all(a["winner"] for a in r["missed"]["items"])
    r30 = client.get("/api/report/weekly?days=30").json()
    assert r30["missed"]["count"] >= r["missed"]["count"]
    from app.bot import texts as T

    text = T.weekly_report(r)
    assert "Мимо вас прошло" in text and len(text) < 4096


def test_competitors(client):
    client.put("/api/me", json={"keywords": ["ремонт кровли", "ремонт", "мебель"], "company_bin": "111111111111"})
    data = client.get("/api/competitors").json()
    assert data["items"] == [] and data["suggest"]
    top = data["suggest"][0]
    assert len(top["bin"]) == 12 and top["wins"] > 0

    assert client.post("/api/competitors", json={"bin": "123"}).status_code == 400
    assert client.post("/api/competitors", json={"bin": "111111111111"}).status_code == 400  # свой БИН
    r = client.post("/api/competitors", json={"bin": top["bin"]})
    assert r.status_code == 200 and r.json()["name"] == top["name"]
    items = client.get("/api/competitors").json()["items"]
    assert len(items) == 1 and items[0]["wins"] >= 1

    d = client.get(f"/api/competitors/{top['bin']}").json()
    assert d["following"] and d["wins"] >= 1 and len(d["months"]) == 12 and d["recent"]
    assert d["overlap"]["wins"] >= 1

    # Новые победы: добавим итог с этим БИН — new_wins его найдёт, повторно — нет
    async def add_and_check():
        from app.db import Award
        from app.services import competitors as rivals

        async with db.Session() as s:
            s.add(Award(source="test", ext_id="x1", title="Ремонт кровли спортзала", winner=top["name"], winner_bin=top["bin"],
                        amount=10_000_000, price=9_000_000, status="done", awarded_at=utcnow()))
            await s.commit()
            first = await rivals.new_wins(s)
            second = await rivals.new_wins(s)
        return first, second
    first, second = asyncio.run(add_and_check())
    assert len(first) == 1 and first[0][2][0].title == "Ремонт кровли спортзала" and second == []

    # Лимит тарифа (пробный: 5)
    for b in ("900000000001", "900000000002", "900000000003", "900000000004"):
        assert client.post("/api/competitors", json={"bin": b}).status_code == 200
    r = client.post("/api/competitors", json={"bin": "900000000005"})
    assert r.status_code == 400 and "лимит" in r.json()["detail"]
    assert client.delete(f"/api/competitors/{top['bin']}").json() == {"ok": True}


def test_onboarding_by_bin(client):
    from app.services.autoprofile import heuristic_keywords

    kws = heuristic_keywords(["Текущий ремонт кровли здания школы", "Ремонт кровли спортзала", "Капитальный ремонт кровли",
                              "Поставка мебели"])
    assert "ремонт кровли" in kws

    assert client.post("/api/onboard/preview", json={"bin": "12"}).status_code == 400
    # Незнакомый БИН в демо-режиме — пример по демо-компании, честно помечен
    r = client.post("/api/onboard/preview", json={"bin": "180940012345"}).json()
    assert r["found"] and r["demo"] and r["demo_company"] and r["wins"] > 0 and r["keywords"]
    assert r["open_now"] >= 1
    # БИН реального победителя из итогов — без пометки демо, с названием
    winner = client.get("/api/competitors").json()["suggest"]
    if winner:
        w = client.post("/api/onboard/preview", json={"bin": winner[0]["bin"]}).json()
        assert w["found"] and not w["demo"] and w["name"] == winner[0]["name"]
    a = client.post("/api/onboard/apply", json={"bin": "180940012345", "keywords": r["keywords"]}).json()
    assert a["user"]["onboarded"] and a["user"]["company_bin"] == "180940012345" and a["stats"]["open"] >= 1


def test_zakup_contracts_to_awards(client, monkeypatch):
    """Договоры zakup.gov.kz (форма ответа снята с сервера 18.11.2026) → итоги, БИН, снижение, рынок по коду ЕНС ТРУ."""
    from app.services import insights, results

    contract = {"id": 116771977, "announcement_name": "Мебель для кабинета информатики", "contract_price_with_vat": 163556.52,
                "customer_organization_name": "КГУ \"Средняя школа №297\"", "customer_organization_id": 7905,
                "supplier_organization_name": "ТОО \"AST 1KZ\"", "supplier_organization_id": 62004606,
                "enstru_code": ["310112.900.000000"], "sign_date": "2026-11-17", "status_name": "Действует",
                "purchase_method_name": "Запрос ценовых предложений"}
    detail = {"buy_total_with_vat": 180000.0,
              "supplier_organization": {"id": 62004606, "iin_bin": "260840013449", "name_ru": "ТОО \"AST 1KZ\""},
              "customer_organization": {"id": 7905, "iin_bin": "120540019776", "name_ru": "КГУ"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, path, params=None):
            if path.startswith("/public/organizations/"):
                return {"results": [{"id": 62004606, "iin_bin": "260840013449", "name_ru": "ТОО \"AST 1KZ\""}]}
            return detail

        async def list(self, path, params, max_items):
            return [contract, {**contract, "id": 2, "contract_price_with_vat": 90000.0, "status_name": "Расторгнут"}]

    monkeypatch.setenv("ZAKUP_ENABLED", "true")
    monkeypatch.setattr(results, "Client", FakeClient)

    async def run():
        async with db.Session() as s:
            org = await results.ensure_supplier(s, "260840013449")
            from app.db import Award, Lot

            a = (await s.execute(select(Award).where(Award.ext_id == "116771977"))).scalar_one()
            lot = Lot(source="zakup", ext_id="L1", title="Парты ученические", amount=200000, raw={"enstru": "310112.900.000047", "customer_org_id": 7905})
            s.add(lot)
            await s.commit()
            ins = await insights.lot_insights(s, lot)
            return org, a, ins
    from sqlalchemy import select

    org, a, ins = asyncio.run(run())
    assert org.org_id == 62004606 and org.bin == "260840013449"
    assert a.winner_bin == "260840013449" and a.customer_bin == "120540019776" and a.amount == 180000.0 and a.enstru.startswith("310112")
    assert ins["available"] and ins["similar_count"] >= 1 and ins["price"]["median_discount"] > 0
    assert ins["customer"]["total"] >= 1 and ins["competitors"][0]["bin"] == "260840013449"
