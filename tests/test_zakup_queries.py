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
