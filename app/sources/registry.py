"""Список источников. Новый источник = новый класс в sources/ + строка здесь."""
from app.sources.demo import DemoSource
from app.sources.goszakup import GoszakupSource

SOURCES = [GoszakupSource(), DemoSource()]
