"""Текстовые примитивы для «умного» поиска: транслит, фонетика, похожесть.

Зачем: голос (ASR) регулярно выдаёт латинское имя кириллицей («Эминем» вместо
«Eminem») или с оговорками («еминем», «эминэм», «eminem»). Обычный поиск ищет
буквально и не находит ничего. Здесь — общие примитивы, которые потом
использует services/query_resolver.py.

Правила:
- translit_ru_en: кириллица → латиница. Таблица та же, что была в playlist_ai,
  но живёт тут одна на весь проект (две таблицы транслита разъезжаются).
- norm_key: нормализация для сравнения (NFKC, lower, хвосты «feat», пунктуация).
- phonetic_key: «звуковой» ключ — только согласные, повторы схлопнуты.
  «Эминем» / «eminem» / «еминем» → «mnm».
- similarity: 0..1 на основе расстояния Левенштейна (без внешних зависимостей).

Всё безопасно на любом входе: мусор на входе → пустой результат, не исключение.
"""
from __future__ import annotations

import re
import unicodedata

# Минимальная таблица RU->EN транслита (для поиска, не для показа).
_RU_EN = str.maketrans({
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
})

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")
_FEAT_RE = re.compile(
    r"\s+(feat\.?|ft\.?|featuring|with|vs\.?|versus|prod\.?)\s+.*$", re.IGNORECASE)
_LATIN_RE = re.compile(r"[^a-z]")
_VOWELS = frozenset("aeiouy")


def translit_ru_en(s: str | None) -> str:
    """Кириллица → латиница (грубо, для поиска). Неизвестные символы — как есть."""
    try:
        t = unicodedata.normalize("NFKC", (s or "")).strip().lower()
        return t.translate(_RU_EN)
    except Exception:
        return (s or "").lower()


def norm_key(s: str | None) -> str:
    """Нормализованная форма для сравнения: lower, без «feat», без пунктуации.

    Порядок важен: сначала убираем пунктуацию (иначе «(feat. X)» не найдётся
    регуляркой — перед «feat» стоит скобка, а не пробел), потом режем
    feat-хвост. «Eminem (feat. Dr. Dre)» → «eminem».
    """
    try:
        t = unicodedata.normalize("NFKC", (s or "")).strip().lower()
        t = _PUNCT_RE.sub(" ", t)
        t = _SPACES_RE.sub(" ", t).strip()
        t = _FEAT_RE.sub("", t)
        return _SPACES_RE.sub(" ", t).strip()
    except Exception:
        return (s or "").strip().lower()


def phonetic_key(s: str | None) -> str:
    """«Звуковой» ключ: согласные без гласных, повторы схлопнуты.

    «Эминем» → «eminem» → «mnm»; «еминем» → то же; «eminem» → «mnm».
    Ловит cases, где транслита мало (выпадение согласного, перестановка).
    """
    try:
        t = _LATIN_RE.sub("", translit_ru_en(s))
        out: list[str] = []
        for ch in t:
            if ch in _VOWELS:
                continue
            if out and out[-1] == ch:
                continue
            out.append(ch)
        return "".join(out)
    except Exception:
        return ""


def levenshtein(a: str, b: str, max_dist: int | None = None) -> int:
    """Расстояние Левенштейна. max_dist — ранний выход (верхняя граница)."""
    try:
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            best = i
            for j, cb in enumerate(b, 1):
                cur.append(min(
                    prev[j] + 1,
                    cur[j - 1] + 1,
                    prev[j - 1] + (0 if ca == cb else 1),
                ))
                if cur[j] < best:
                    best = cur[j]
            prev = cur
            if max_dist is not None and best > max_dist:
                return max_dist + 1
        return prev[-1]
    except Exception:
        return max(len(a or ""), len(b or ""))


def similarity(a: str | None, b: str | None) -> float:
    """Похожесть двух строк: 1.0 — совпадение, 0.0 — ничего общего."""
    x = (a or "").strip().lower()
    y = (b or "").strip().lower()
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    try:
        d = levenshtein(x, y)
        m = max(len(x), len(y))
        return max(0.0, 1.0 - (d / m if m else 1.0))
    except Exception:
        return 0.0
