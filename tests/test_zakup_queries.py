from app.sources.zakup import remote_queries
from app.services.matcher import phrase_matches, words


def test_remote_queries_use_word_stems():
    assert remote_queries(["мебель"]) == ["мебе"]
    assert remote_queries(["поставка мебели"]) == ["мебе"]
    q = remote_queries(["ремонт кровли"])[0]
    assert q in "ремонт кровли"
    assert remote_queries(["кабель", "кабель"]) == ["кабе"]


def test_phrase_still_filtered_locally():
    assert phrase_matches("ремонт кровли", words("Текущий ремонт кровельного покрытия школы"))
    assert not phrase_matches("ремонт кровли", words("Кровельные материалы"))


def test_parse_keywords_drops_fragments():
    from app.services.matcher import parse_keywords
    assert parse_keywords(["9", "0", "4", "4", "6", "5"]) == []
    assert parse_keywords("904465, мебель") == ["мебель"]
    assert parse_keywords(["Ремонт  кровли", "мебель", "мебель"]) == ["ремонт кровли", "мебель"]
