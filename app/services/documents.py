"""Библиотека документов компании и «готовность к заявке».

Готовность: для каждого документа, который требует закупка (из ИИ-разбора), ищем
подходящий в библиотеке по смыслу слов (с учётом окончаний) и по типу документа,
и проверяем срок действия на дату окончания приёма заявок.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import DOC_KINDS, Document, aware, utcnow
from app.services.matcher import query_stems, words

MAX_SIZE = 15 * 1024 * 1024
ALLOWED_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".zip", ".rar", ".txt"}
EXPIRING = timedelta(days=30)

# Подсказки типа документа по словам требования
KIND_HINTS = {
    "guarantee": ["гарант", "обеспечен"],
    "tax": ["налог", "задолжен"],
    "bank": ["банк", "ссудн"],
    "license": ["лиценз", "разрешен"],
    "registration": ["регистрац"],
    "charter": ["устав", "учредит"],
    "certificate": ["сертифик", "iso", "ст-kz", "соответств"],
    "experience": ["опыт", "акт", "договор"],
    "staff": ["квалифик", "персонал", "специалист", "диплом"],
    "equipment": ["техник", "оборудован", "транспорт"],
}


def doc_status(doc: Document, on: datetime | None = None) -> str:
    """valid / expiring / expired / no_term."""
    exp = aware(doc.expires_at)
    if exp is None:
        return "no_term"
    on = on or utcnow()
    if exp < on:
        return "expired"
    if exp - on <= EXPIRING:
        return "expiring"
    return "valid"


def doc_json(d: Document) -> dict:
    return {
        "id": d.id, "title": d.title, "kind": d.kind, "kind_name": DOC_KINDS.get(d.kind, "Прочее"),
        "filename": d.filename, "size": d.size, "mime": d.mime,
        "issued_at": aware(d.issued_at).isoformat() if d.issued_at else None,
        "expires_at": aware(d.expires_at).isoformat() if d.expires_at else None,
        "note": d.note, "status": doc_status(d), "created_at": aware(d.created_at).isoformat(),
    }


def guess_kind(text: str) -> str | None:
    low = text.lower()
    for kind, hints in KIND_HINTS.items():
        if any(h in low for h in hints):
            return kind
    return None


def best_match(requirement: str, docs: list[Document]) -> Document | None:
    """Документ из библиотеки, который закрывает требование, или None."""
    req_stems = query_stems(requirement)
    kind = guess_kind(requirement)
    best, best_score = None, 0.0
    for d in docs:
        doc_words = words(f"{d.title} {d.note} {DOC_KINDS.get(d.kind, '')}")
        overlap = sum(1 for st in req_stems if any(w.startswith(st) for w in doc_words))
        score = overlap / max(1, len(req_stems))
        if kind and d.kind == kind:
            score += 0.6
        elif kind and d.kind != "other":
            score -= 0.3  # документ другого типа почти наверняка не подходит
        if score > best_score:
            best, best_score = d, score
    return best if best_score >= 0.5 else None


async def list_docs(s: AsyncSession, user_id: int) -> list[Document]:
    return list((await s.execute(
        select(Document).where(Document.user_id == user_id).order_by(Document.expires_at.is_(None), Document.expires_at)
    )).scalars().all())


async def readiness(s: AsyncSession, user_id: int, required: list[str], deadline: datetime | None) -> dict:
    """Матрица соответствия: [{требование, документ, статус}] + итог."""
    docs = await list_docs(s, user_id)
    on = aware(deadline) or utcnow()
    rows, ready = [], 0
    for req in required:
        d = best_match(req, docs)
        if d is None:
            status = "missing"
        else:
            # На дату окончания приёма заявок документ должен действовать
            status = "expired" if doc_status(d, on) == "expired" else "ready"
        ready += status == "ready"
        rows.append({"requirement": req, "status": status, "document": doc_json(d) if d else None})
    return {"items": rows, "ready": ready, "total": len(required)}
