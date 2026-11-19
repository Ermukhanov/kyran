"""Поиск первых пользователей Kyran: кто выигрывает закупки у заказчиков Актобе.

Берём организации-заказчики с «Актобе/Ақтөбе/Актюбинск» в названии, их договоры за год
и считаем, какие поставщики выигрывают чаще всего. Результат — в лог (разовый запуск).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta

from app.services.results import Client, enabled

log = logging.getLogger(__name__)

QUERIES = ("Актобе", "Ақтөбе", "Актюбинск")


def _rows(data):
    return (data.get("results") if isinstance(data, dict) else data) or []


async def find_aktobe_suppliers(top: int = 45, customers_max: int = 70) -> list[dict]:
    if not enabled():
        return []
    since = (date.today() - timedelta(days=365)).isoformat()
    async with Client() as c:
        customers: dict[int, str] = {}
        sample_logged = False
        for q in QUERIES:
            try:
                for org in await c.list("/public/organizations/", {"q": q}, 60):
                    if not sample_logged:
                        log.info("Лиды: поля организации: %s", sorted(org.keys()))
                        sample_logged = True
                    if org.get("id"):
                        customers[int(org["id"])] = str(org.get("name_ru") or org.get("name") or "")
            except Exception as e:
                log.warning("Лиды: заказчики «%s»: %s", q, e)
        log.info("Лиды: заказчиков Актобе найдено %d", len(customers))

        wins: dict[int, dict] = defaultdict(lambda: {"n": 0, "sum": 0.0, "name": "", "titles": [], "customers": set()})
        for cid in list(customers)[:customers_max]:
            try:
                rows = await c.list("/public/contracts/", {"customer_organization_id": cid, "sign_date__gte": since}, 100)
            except Exception as e:
                log.warning("Лиды: договоры %s: %s", cid, e)
                continue
            for r in rows:
                sid = r.get("supplier_organization_id")
                if not sid:
                    continue
                w = wins[int(sid)]
                w["n"] += 1
                try:
                    w["sum"] += float(r.get("contract_price_with_vat") or 0)
                except (TypeError, ValueError):
                    pass
                w["name"] = str(r.get("supplier_organization_name") or w["name"])
                t = str(r.get("announcement_name") or r.get("description_ru") or "")[:60]
                if t and len(w["titles"]) < 2 and t not in w["titles"]:
                    w["titles"].append(t)
                w["customers"].add(cid)

        ranked = sorted(wins.items(), key=lambda kv: (-len(kv[1]["customers"]), -kv[1]["n"]))[:top]
        out = []
        detail_logged = False
        for sid, w in ranked:
            info = {"org_id": sid, "name": w["name"], "contracts": w["n"], "customers": len(w["customers"]),
                    "sum": round(w["sum"]), "examples": w["titles"], "bin": "", "contacts": ""}
            try:
                d = await c.get(f"/public/organizations/{sid}/")
                if isinstance(d, dict):
                    if not detail_logged:
                        log.info("Лиды: поля карточки организации: %s", sorted(d.keys()))
                        detail_logged = True
                    info["bin"] = str(d.get("iin_bin") or d.get("bin") or "")
                    bits = []
                    for k in ("phone", "phones", "email", "emails", "address_ru", "address", "kato_name_ru", "website"):
                        v = d.get(k)
                        if v:
                            bits.append(f"{k}={v}")
                    info["contacts"] = "; ".join(bits)[:300]
            except Exception as e:
                info["contacts"] = f"нет карточки: {e}"
            out.append(info)
    for i, x in enumerate(out, 1):
        log.info("Лид %02d | %s | БИН %s | договоров %d у %d заказчиков Актобе | %s ₸ | %s | %s",
                 i, x["name"], x["bin"], x["contracts"], x["customers"], f'{x["sum"]:,}'.replace(",", " "),
                 " / ".join(x["examples"]), x["contacts"])
    return out
