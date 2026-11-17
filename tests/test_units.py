import hashlib
import hmac
import json
import time
from datetime import datetime
from urllib.parse import urlencode

import pytest

from app.ai.client import extract_json
from app.api.auth import make_link_token, read_link_token, validate_init_data
from app.services.matcher import Profile, keyword_hit, parse_keywords, phrase_matches, words
from app.sources.goszakup import GoszakupSource, item_to_lot, parse_dt
from app.sources.website import fingerprint, is_public_url, page_text


# ── Подбор ──
@pytest.mark.parametrize("query,text,ok", [
    ("ремонт кровли", "Текущий ремонт кровельного покрытия школы", True),
    ("ремонт кровли", "Ремонт спортзала", False),
    ("бумага а4", "Бумага офисная формата А4", True),
    ("канцтовары", "Приобретение канцтоваров", True),
    ("мебель", "Мебельная продукция", True),
    ("поставка мебели", "Мебель ученическая", True),  # «поставка» — стоп-слово
    ("жөндеу", "Мектеп шатырын жөндеу", True),
])
def test_phrase_matches(query, text, ok):
    assert phrase_matches(query, words(text)) is ok


def test_parse_keywords():
    assert parse_keywords("Ремонт кровли, канцтовары;\n бумага А4 ,,") == ["ремонт кровли", "канцтовары", "бумага а4"]


def test_keyword_hit_scores_and_filters():
    p = Profile(keywords=["мебель", "ремонт"], min_amount=5e6, max_amount=5e7)
    assert keyword_hit("Мебель для школы", "", 1e6, "", p) is None          # меньше минимума
    hit = keyword_hit("Мебель для школы", "", 1e7, "", p)
    assert hit and 55 <= hit.score <= 95 and "мебель" in hit.reason
    assert keyword_hit("Мебель", "", None, "", p) is not None                # сумма неизвестна — не отсекаем
    both = keyword_hit("Ремонт и мебель", "", 1e7, "", p)
    assert both.score > hit.score


def test_region_filter():
    p = Profile(keywords=["мебель"], regions=["Актюбинская"])
    assert keyword_hit("Мебель", "", None, "Актюбинская область", p)
    assert keyword_hit("Мебель", "", None, "г. Алматы", p) is None


# ── goszakup ──
def test_goszakup_disabled_without_token():
    assert not GoszakupSource(token="").enabled
    assert GoszakupSource(token="x").enabled


def test_item_to_lot():
    lot = item_to_lot({
        "id": 555, "trd_buy_id": 777, "name_ru": " Ремонт кровли ", "amount": "12 500 000,50",
        "end_date": "2026-11-20 18:00:00", "files": [{"original_name": "ТС.pdf", "file_path": "https://x/1.pdf"}],
    })
    assert lot.ext_id == "555" and lot.title == "Ремонт кровли"
    assert lot.amount == pytest.approx(12500000.5)
    assert lot.deadline.hour == 18 and lot.deadline.tzinfo is not None
    assert lot.url.endswith("/announce/index/777")
    assert lot.documents == [{"name": "ТС.pdf", "url": "https://x/1.pdf"}]


def test_bad_values_do_not_crash():
    lot = item_to_lot({"id": 1, "name_ru": "X", "amount": "abc", "end_date": "не дата"})
    assert lot.amount is None and lot.deadline is None
    assert parse_dt("20.11.2026 18:00").day == 20


# ── Сайты ──
def test_page_text_and_fingerprint():
    title, text, links = page_text("<html><head><title>Тендеры</title><script>x()</script></head>"
                                   "<body><a href='/t/1'>Закупка мебели</a><p>Срок 12:30</p></body></html>")
    assert title == "Тендеры" and "x()" not in text and ("Закупка мебели", "/t/1") in links
    assert fingerprint("Срок 12:30") == fingerprint("Срок 14:45")  # время не считается изменением


def test_private_urls_blocked():
    assert not is_public_url("http://localhost:8000")
    assert not is_public_url("http://127.0.0.1/")
    assert not is_public_url("file:///etc/passwd")


# ── Авторизация ──
def signed_init_data(bot_token: str, user_id: int = 42, auth_date: int | None = None) -> str:
    fields = {"auth_date": str(auth_date or int(time.time())), "user": json.dumps({"id": user_id, "first_name": "Ерлан"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_init_data_valid_and_tampered():
    data = signed_init_data("123:abc")
    assert validate_init_data(data, "123:abc")["id"] == 42
    with pytest.raises(ValueError):
        validate_init_data(data, "123:other")
    with pytest.raises(ValueError):
        validate_init_data(signed_init_data("123:abc", auth_date=int(time.time()) - 10 * 86400), "123:abc")


def test_link_token():
    assert read_link_token(make_link_token(99)) == 99
    with pytest.raises(ValueError):
        read_link_token(make_link_token(99)[:-2] + "00")
    with pytest.raises(ValueError):
        read_link_token(make_link_token(99, ttl=-1))


# ── ИИ-ответы ──
def test_extract_json():
    assert extract_json('Вот:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('ok {"scores": [1]} end') == {"scores": [1]}
    with pytest.raises(ValueError):
        extract_json("нет json")
