# Kyran — ИИ-агент для госзакупок

Kyran каждый день просматривает тендеры, присылает компании только подходящие,
разбирает документацию и напоминает о дедлайнах. Работает в Telegram: бот и
Mini App на телефоне, ПК и в браузере.

Стратегия, конкуренты и план: [docs/STRATEGY.md](docs/STRATEGY.md).

## Что умеет MVP

| Функция | Без ключей | С `ANTHROPIC_API_KEY` |
| --- | --- | --- |
| Онбординг за минуту (направления, суммы, БИН) | ✅ | ✅ |
| Подбор тендеров | по словам с учётом окончаний (RU/KZ) | + ИИ-оценка 0–100 с объяснением |
| Срочные уведомления (≤3 дней) и дайджест в 9:00 | ✅ | ✅ |
| Mini App: лента, «фитиль дедлайна», поиск, фильтры | ✅ | ✅ |
| Воронка: Интересно → Готовим → Подали → Выиграли/Проиграли + заметки | ✅ | ✅ |
| ИИ-разбор: вердикт, требования со ссылкой на пункт, чек-лист документов, риски | — | ✅ |
| Чат с агентом обычными словами («найди ремонт школ до 50 млн и следи») | поиск по словам | ✅ инструменты |
| Мониторинг любого сайта по ссылке | уведомление об изменении | + ИИ вытаскивает новые закупки |
| Вход с ПК: команда `/web` → личная ссылка | ✅ | ✅ |

Реальные тендеры пойдут, когда будет `GOSZAKUP_TOKEN` (токен Минфина к ows.goszakup.gov.kz).
До этого `DEMO_MODE=true` показывает вымышленные демо-лоты с пометкой «ДЕМО» — для встреч с клиентами.

## Деплой на Railway

1. **New Project → Deploy from GitHub repo → `Ermukhanov/kyran`.**
2. В проекте: **+ Create → Database → PostgreSQL**.
3. В сервисе `kyran` → **Settings → Networking → Generate Domain** (нужен для Mini App).
4. В сервисе `kyran` → **Variables**:
   - `TELEGRAM_BOT_TOKEN` — токен бота
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `SECRET_KEY` — любая длинная случайная строка
   - `DEMO_MODE` = `true` (пока нет токена госзакупок)
   - `ADMIN_IDS` — ваш Telegram ID
   - позже: `ANTHROPIC_API_KEY`, `GOSZAKUP_TOKEN`
5. Деплой пойдёт сам. Проверка: `https://<домен>/health` → `{"ok": true, ...}`, в логах — `Telegram-бот запущен`.
6. В Telegram: `/start` у бота. Кнопка «Kyran» появится в меню бота автоматически.

> ⚠️ Бот должен работать **в одном экземпляре** (polling). Не включайте несколько реплик.

## Когда пришёл токен госзакупок

1. Добавьте `GOSZAKUP_TOKEN`, поставьте `DEMO_MODE=false`.
2. В логах: `goszakup: получено N лотов`.
3. Если N = 0 или ошибка — сверьте поля в `app/sources/goszakup.py` → `item_to_lot()` с реальным
   ответом `GET https://ows.goszakup.gov.kz/v3/lots` и эндпоинт карточки участника в `company()`.

## Структура

```
app/
├── main.py           # FastAPI + запуск бота и фоновых задач
├── config.py         # все переменные окружения
├── db.py             # модели: users, lots, matches, pipeline, analyses, monitors
├── sources/          # goszakup (API), demo, website (любой сайт), registry
├── services/         # matcher, core (подбор, лента, воронка), monitors, analyses
├── ai/               # client, scoring (Haiku), analysis (Sonnet), agent (инструменты)
├── bot/              # Telegram: онбординг, команды, агент-чат, уведомления
└── api/              # REST для Mini App + вход через Telegram initData / ссылку /web
web/                  # Mini App: index.html, app.css, app.js (без сборки)
tests/                # pytest
```

Новый источник = класс с `name`, `enabled`, `fetch()` в `app/sources/` + строка в `registry.py`.

## Локально

```bash
pip install -r requirements-dev.txt
pytest
DEMO_MODE=true SECRET_KEY=dev uvicorn app.main:app --reload   # без TELEGRAM_BOT_TOKEN работает только веб
```

Модели ИИ меняются переменными `AI_FAST_MODEL` и `AI_SMART_MODEL` (по умолчанию обе — лёгкая `claude-haiku-5-5`). Расход токенов пишется в логи строками «ИИ …: вход N, выход M токенов».
