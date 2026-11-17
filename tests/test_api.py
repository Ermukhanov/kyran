"""Сквозной тест: демо-лоты → профиль → лента → воронка → разбор (ИИ замокан)."""
import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.api.auth import make_link_token


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.configure(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(config, "COLLECT_EVERY_MIN", 60)
    from app.main import app

    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer " + make_link_token(1001)
        import time
        for _ in range(50):  # ждём первый сбор демо-лотов
            if c.get("/api/search?q=ремонт").json()["items"]:
                break
            time.sleep(0.1)
        yield c


def test_requires_auth(client):
    r = client.get("/api/me", headers={"Authorization": ""})
    assert r.status_code == 401


def test_full_flow(client, monkeypatch):
    me = client.get("/api/me").json()
    assert me["features"] == {"ai": False, "demo": True, "sources": {"zakup": False, "goszakup": False, "demo": True}}
    assert me["user"]["onboarded"] is False

    r = client.put("/api/me", json={"keywords": ["ремонт кровли", "мебель"], "min_amount": None, "max_amount": None})
    assert r.status_code == 200
    assert r.json()["stats"]["open"] >= 2

    items = client.get("/api/feed").json()["items"]
    titles = [i["title"] for i in items]
    assert any("кровли" in t for t in titles) and any("мебел" in t.lower() for t in titles)
    assert all(i["is_demo"] for i in items)
    assert items == sorted(items, key=lambda i: -i["score"])

    lot_id = items[0]["id"]
    assert client.post(f"/api/lots/{lot_id}/stage", json={"stage": "preparing", "note": "цена 35 млн"}).json() == {"stage": "preparing"}
    assert client.post(f"/api/lots/{lot_id}/stage", json={"stage": "bogus"}).status_code == 400
    board = client.get("/api/pipeline").json()["columns"]
    assert board["preparing"][0]["id"] == lot_id and board["preparing"][0]["note"] == "цена 35 млн"

    detail = client.get(f"/api/lots/{lot_id}").json()
    assert detail["lot"]["stage"] == "preparing" and detail["analysis"] is None

    # Без ключа — честный отказ
    r = client.post(f"/api/lots/{lot_id}/analyze")
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]

    # С «ИИ» (замокан) — разбор сохраняется и обновляет оценку
    from app.ai import analysis, client as ai

    async def fake_ask(model, system, prompt, max_tokens=4000):
        return {"verdict": "go", "score": 91, "summary": "Подходит", "requirements": [{"text": "Лицензия", "source": "п. 2"}],
                "documents": ["Справка об отсутствии налоговой задолженности"], "risks": [{"text": "Сжатые сроки", "level": "high"}]}

    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "ask_json", fake_ask)
    a = client.post(f"/api/lots/{lot_id}/analyze").json()["analysis"]
    assert a["verdict"] == "go" and a["docs_used"] == []
    assert client.get(f"/api/lots/{lot_id}").json()["lot"]["score"] == 91

    # Скрыть
    client.post(f"/api/lots/{lot_id}/hide")
    assert lot_id not in [i["id"] for i in client.get("/api/feed").json()["items"]]


def test_profile_change_rematches(client):
    client.put("/api/me", json={"keywords": "мебель"})
    n1 = client.get("/api/me").json()["stats"]["open"]
    client.put("/api/me", json={"keywords": "мебель, бумага, ремонт"})
    n2 = client.get("/api/me").json()["stats"]["open"]
    assert n2 > n1


def test_monitor_rejects_private(client):
    r = client.post("/api/monitors", json={"url": "http://127.0.0.1:8000"})
    assert r.status_code == 400


def test_static(client):
    assert "Kyran" in client.get("/app").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/health").json()["ok"]
