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
