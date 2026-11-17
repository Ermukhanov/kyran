"""Бот собирается без сети; ИИ-агент корректно гоняет цикл инструментов (Anthropic замокан)."""
import asyncio
from types import SimpleNamespace

from app import config, db
from app.bot import texts as T


def test_bot_builds(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:TEST")
    from app.bot.bot import build

    application = build()
    assert application.handlers[0]


def test_texts():
    assert T.money(12_500_000) == "12 500 000 ₸"
    assert T.money(38_400_000, short=True) == "38,4 млн ₸"
    assert T.money(40_000_000, short=True) == "40 млн ₸"
    from app.db import Lot

    lot = Lot(id=1, source="t", ext_id="1", title="Ремонт <b>", customer="A & B", is_demo=True, amount=1e6)
    card = T.card(lot, 90, "почему")
    assert "&lt;b&gt;" in card and "A &amp; B" in card and "Демонстрационная" in card


def test_agent_tool_loop(tmp_path, monkeypatch):
    db.configure(f"sqlite+aiosqlite:///{tmp_path}/a.db")
    from app.ai import agent, client as ai
    from app.services import core
    from app.sources.demo import DemoSource

    monkeypatch.setattr(config, "DEMO_MODE", True)
    calls = []

    class FakeMessages:
        async def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                block = SimpleNamespace(type="tool_use", id="t1", name="add_keywords", input={"keywords": ["ремонт кровли"]})
                return SimpleNamespace(stop_reason="tool_use", content=[block])
            if len(calls) == 2:
                block = SimpleNamespace(type="tool_use", id="t2", name="search_tenders", input={"query": "кровля ремонт"})
                return SimpleNamespace(stop_reason="tool_use", content=[block])
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Нашёл 1 тендер.")])

    monkeypatch.setattr(ai, "client", lambda: SimpleNamespace(messages=FakeMessages()))

    async def run():
        await db.init_db()
        async with db.Session() as s:
            await core.collect(s, [DemoSource()])
            user = await core.get_or_create_user(s, 5, "Тест")
            answer, run_ = await agent.respond(s, user, [], "Следи за ремонтом кровли и найди сейчас")
            await s.refresh(user)
            return answer, run_, user

    answer, run_, user = asyncio.run(run())
    assert answer == "Нашёл 1 тендер."
    assert user.keywords == ["ремонт кровли"] and user.onboarded
    assert run_.shown_lots
    # результат инструмента ушёл модели
    results = [c for m in calls[-1]["messages"] if isinstance(m["content"], list) for c in m["content"]
               if isinstance(c, dict) and c.get("type") == "tool_result"]
    assert len(results) == 2 and "matched_now" in results[0]["content"]


def test_login_handlers_take_priority(tmp_path, monkeypatch):
    """Код входа и ссылка с сайта обрабатываются до онбординга (группа -1)."""
    import asyncio
    from types import SimpleNamespace

    from telegram.ext import ApplicationHandlerStop

    from app import config, db
    from app.bot import bot as B
    from app.services import login

    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
    app = B.build()
    early = app.handlers[-1]
    assert len(early) == 3  # ссылка входа, код входа, файлы с ТЗ
    assert B.LOGIN_CODE.match("123 456") and B.LOGIN_CODE.match("123456") and not B.LOGIN_CODE.match("1234567")

    db.configure(f"sqlite+aiosqlite:///{tmp_path}/b.db")
    sent = []

    async def reply_text(text, **kw):
        sent.append(text)

    def upd(text):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=555, full_name="Тест", username="t"),
            effective_message=SimpleNamespace(reply_text=reply_text),
            message=SimpleNamespace(text=text),
        )

    async def flow():
        await db.init_db()
        async with db.Session() as s:
            lc = await login.start(s)
        try:
            await B.login_code_handler(upd(lc.code[:3] + " " + lc.code[3:]), SimpleNamespace(args=[]))
        except ApplicationHandlerStop:
            pass
        async with db.Session() as s:
            return await login.poll(s, lc.nonce)

    assert asyncio.run(flow())
    assert "подтверждён" in sent[0]


def test_upload_tz_flow(tmp_path, monkeypatch):
    """Присланное ТЗ (Word) → личная закупка с разбором, видна только владельцу."""
    import asyncio
    import io
    import json
    from types import SimpleNamespace

    from docx import Document as Docx
    from fastapi.testclient import TestClient

    from app import db
    from app.ai import client as ai
    from app.api.auth import make_link_token
    from app.services import uploads

    d = Docx()
    d.add_paragraph("Техническая спецификация. Текущий ремонт кровли здания школы №12. Площадь 1450 м2. " * 20)
    buf = io.BytesIO(); d.save(buf)
    files = [("ТЗ кровля.docx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")]
    prep = uploads.prepare(files)
    assert "1450" in prep.text and not prep.blocks
    try:
        uploads.prepare([("old.doc", b"\xd0\xcf\x11\xe0", "application/msword")])
        assert False
    except uploads.UploadError as e:
        assert ".docx" in str(e)

    answer = {"verdict": "go", "score": 88, "summary": "Подходит.", "scope": ["Ремонт кровли 1450 м²"],
              "requirements": [{"text": "Лицензия СМР", "source": "п. 2"}], "documents": ["Справка об отсутствии налоговой задолженности"],
              "risks": [{"text": "Зимний период", "level": "high"}], "questions": ["Срок?"], "next_steps": [],
              "lot": {"title": "Текущий ремонт кровли школы №12", "customer": "КГУ «Школа №12»", "amount": "38 400 000",
                      "deadline": "2030-12-01T18:00", "region": "Актюбинская область", "method": "Конкурс", "number": "123-1"}}
    seen = {}

    async def create(**kw):
        seen.update(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(answer, ensure_ascii=False))], usage=None)

    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))

    db.configure(f"sqlite+aiosqlite:///{tmp_path}/u.db")
    from app.main import app

    with TestClient(app) as c:
        async def run():
            async with db.Session() as s:
                from app.services import core

                u = await core.get_or_create_user(s, 4004, "Тест")
                return await uploads.process(s, u, files)
        lot, data = asyncio.run(run())
        assert lot.title == "Текущий ремонт кровли школы №12" and lot.amount == 38_400_000 and lot.deadline
        assert "1450" in seen["messages"][0]["content"][-1]["text"]
        from app.bot import texts as T

        report = T.upload_report(lot, data)
        assert "Рекомендуем участвовать · 88%" in report and "Лицензия СМР" in report and len(report) < 4096

        owner = {"Authorization": "Bearer " + make_link_token(4004)}
        other = {"Authorization": "Bearer " + make_link_token(5005)}
        r = c.get(f"/api/lots/{lot.id}", headers=owner).json()
        assert r["analysis"]["verdict"] == "go" and r["lot"]["stage"] == "saved"
        assert c.get(f"/api/lots/{lot.id}", headers=other).status_code == 404
        assert all(i["id"] != lot.id for i in c.get("/api/search?q=кровли", headers=other).json()["items"])
