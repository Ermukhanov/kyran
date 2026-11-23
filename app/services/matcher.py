"""Быстрый подбор без ИИ: слова с учётом окончаний (RU/KZ), сумма, регион.

«ремонт кровли» найдёт «Ремонт кровельного покрытия»: у слов запроса отрезаем
окончание (2 буквы, минимум 4 остаётся) и ищем слова лота с этой основой.
Это первый фильтр. Сверху ИИ ставит точную оценку 0–100 (если есть ключ).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-zа-яёәғқңөұүһі0-9]+", re.IGNORECASE)
MIN_STEM = 4
STOP = {"для", "или", "под", "при", "без", "услуги", "услуг", "поставка", "приобретение", "закупка", "работы"}


def _norm(w: str) -> str:
    return w.lower().replace("ё", "е")


def _stem(w: str) -> str:
    w = _norm(w)
    return w[: max(MIN_STEM, len(w) - 2)] if len(w) > MIN_STEM else w


def words(text: str) -> set[str]:
    return {_norm(w) for w in _WORD.findall(text or "") if len(w) >= 3}


def query_stems(text: str) -> set[str]:
    stems = {_stem(w) for w in _WORD.findall(text or "") if len(w) >= 3}
    meaningful = {s for s in stems if s not in {_stem(x) for x in STOP}}
    return meaningful or stems


def parse_keywords(text) -> list[str]:
    """«ремонт кровли, канцтовары; бумага А4» → ['ремонт кровли', 'канцтовары', 'бумага а4'].
    Принимает строку или список. Отбрасывает обрывки короче 3 символов и голые числа:
    по ним площадка ничего не находит (было: направления «9», «0», «4»… по одной цифре)."""
    if isinstance(text, (list, tuple, set)):
        text = ", ".join(str(x) for x in text)
    out: list[str] = []
    for part in re.split(r"[,;\n]+", str(text or "")):
        k = " ".join(part.split()).lower()
        if len(k) >= 3 and not k.replace(" ", "").isdigit() and k not in out:
            out.append(k)
    return out[:30]


def phrase_matches(phrase: str, lot_words: set[str]) -> bool:
    stems = query_stems(phrase)
    return bool(stems) and all(any(w.startswith(s) for w in lot_words) for s in stems)


@dataclass
class Profile:
    keywords: list[str] = field(default_factory=list)
    min_amount: float | None = None
    max_amount: float | None = None
    regions: list[str] = field(default_factory=list)


@dataclass
class Hit:
    score: int          # 0–100
    reason: str


# Регион пользователь пишет как угодно: «Актюбинская», «Актобе», «Ақтөбе облысы».
# На площадке в адресе поставки: «Актюбинская область, г.Актобе, …» или «область Абай, …».
REGION_ALIASES = {
    "актюб": ["актюб", "актобе", "ақтөбе"], "актобе": ["актюб", "актобе", "ақтөбе"], "ақтөбе": ["актюб", "актобе", "ақтөбе"],
    "алматы": ["алматы", "алма-ат"], "алмат": ["алматин", "алматы", "алма"], "астан": ["астана", "нур-султан", "нұр-сұлтан"],
    "шымкент": ["шымкент"], "атырау": ["атырау"], "мангист": ["мангист", "маңғыст", "актау", "ақтау"],
    "актау": ["мангист", "актау", "ақтау"], "караганд": ["караганд", "қарағанд"], "костан": ["костанай", "қостанай"],
    "павлодар": ["павлодар"], "кызылорд": ["кызылорд", "қызылорд"], "туркест": ["туркестан", "түркістан"],
    "жамбыл": ["жамбыл", "тараз"], "тараз": ["жамбыл", "тараз"], "уральск": ["западно-казах", "уральск", "орал"],
    "западно": ["западно-казах", "уральск", "орал"], "восточно": ["восточно-казах", "усть-камен", "өскемен"],
    "северо": ["северо-казах", "петропавл"], "петропавл": ["северо-казах", "петропавл"], "акмол": ["акмолин", "ақмола", "кокшет", "көкшет"],
    "абай": ["абай", "семей"], "семей": ["абай", "семей"], "жетіс": ["жетіс", "жетыс", "талдыкорг"], "жетыс": ["жетіс", "жетыс", "талдыкорг"],
    "ұлытау": ["ұлытау", "улытау", "жезказг"], "улытау": ["ұлытау", "улытау", "жезказг"],
}


def region_matches(wanted: str, region: str) -> bool:
    w, r = _norm(wanted).strip(), _norm(region)
    if not w:
        return True
    if w in r or (len(r) > 3 and r in w):
        return True
    for word in _WORD.findall(w):
        if len(word) < 4 or word.startswith(("облас", "облыс", "город", "район", "аудан", "респуб", "казах")):
            continue
        for key, variants in REGION_ALIASES.items():
            if word.startswith(key) or (len(word) >= 5 and key.startswith(word[:5])):
                if any(v in r for v in variants):
                    return True
        stem = _stem(word)
        if len(stem) >= 5 and stem in r:
            return True
    return False


def passes_filters(amount: float | None, region: str, p: Profile) -> bool:
    if amount is not None:
        if p.min_amount is not None and amount < p.min_amount:
            return False
        if p.max_amount is not None and amount > p.max_amount:
            return False
    if p.regions:
        if not region or not any(region_matches(r, region) for r in p.regions):
            return False
    return True


def keyword_hit(title: str, description: str, amount: float | None, region: str, p: Profile) -> Hit | None:
    """None — не подходит. Иначе оценка 0–100 и причина."""
    if not p.keywords or not passes_filters(amount, region, p):
        return None
    title_w = words(title)
    all_w = title_w | words(description)
    in_title = [k for k in p.keywords if phrase_matches(k, title_w)]
    in_desc = [k for k in p.keywords if k not in in_title and phrase_matches(k, all_w)]
    if not in_title and not in_desc:
        return None
    score = min(95, 55 + 20 * len(in_title) + 8 * len(in_desc))
    found = in_title + in_desc
    reason = "Совпадает с вашими направлениями: " + ", ".join(found[:3])
    return Hit(score=score, reason=reason)
