"""Шаблонные названия плейлистов и пояснение «почему такой микс».

Зачем без LLM. Как в Яндекс Музыке: для миллионов плейлистов чистый LLM
слишком дорог и медленный. Здесь всё считается за миллисекунды — словарь
признаков плюс склонение под наши жанры, без внешних NLP-зависимостей
(pymorphy2 в образе нет, а пересборка образа ради названий — лишнее).

Используется в двух местах:
  * как быстрый путь, когда ИИ-провайдер не настроен или провалился — раньше
    в этом случае имя было «Микс: <запрос>», что выглядит как заглушка;
  * для поля comment: собираем «Потому что ...» из доминирующих жанров и
    настроений, иначе это самое интересное в плейлисте не объяснялось.

Словарь сознательно маленький: жанры приходят из ID3, настроения — из
sonic/CLAP-анализа, то есть множество значений ограничено.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

# Английские муд-лейблы -> русские прилагательные для названий.
MOOD_RU: dict[str, str] = {
    "calm": "спокойное",
    "relaxed": "спокойное",
    "peaceful": "мирное",
    "sleepy": "сонное",
    "dreamy": "мечтательное",
    "melancholic": "меланхоличное",
    "sad": "грустное",
    "dark": "мрачное",
    "aggressive": "агрессивное",
    "energetic": "бодрое",
    "excited": "взрывное",
    "happy": "весёлое",
    "playful": "игривое",
    "romantic": "романтичное",
    "s sensual": "чувственное",
    "sensual": "чувственное",
    "epic": "эпичное",
    "atmospheric": "атмосферное",
    "focused": "сосредоточенное",
    "chill": "чиловое",
    "groovy": "groovy",
}

# Частые жанры -> русский вариант. Неизвестные отдаём как есть.
GENRE_RU: dict[str, str] = {
    "rock": "рока", "pop": "попа", "jazz": "джаза", "metal": "метала",
    "punk": "панка", "indie": "инди", "folk": "фолка", "soul": "соула",
    "funk": "фанка", "blues": "блюза", "classical": "классики",
    "electronic": "электроники", "house": "хауса", "techno": "техно",
    "ambient": "амбиента", "trip-hop": "трип-хопа", "hip hop": "хип-хопа",
    "hip-hop": "хип-хопа", "r&b": "R&B", "reggae": "регги",
    "disco": "диско", "alternative": "альтернативы", "indie rock": "инди-рока",
    "synthpop": "синтпопа", "lo-fi": "лоу-фай", "lounge": "лаунжа",
    "orchestral": "оркестровой музыки", "drum and bass": "drum and bass",
    "shoegaze": "шугейза", "post-punk": "пост-панка", "post-rock": "пост-рока",
    "math rock": "мат-рока", "progressive rock": "прогрессив-рока",
    "soundtrack": "саундтреков", "classical crossover": "классик-кроссовера",
    "шансон": "шансона", "русский рок": "русского рока", "одесский джаз": "одесского джаза",
}

# Слова, которые не склоняем (англицизмы, аббревиатуры).
_NO_INFL = {"r&b", "dnb", "idm", "k-pop", "hip-hop", "drone", "wave", "lo-fi",
            "drum and bass", "ambient", "techno", "house", "funk", "punk", "metal"}


def _strip(s: Any) -> str:
    return str(s or "").strip().lower()


def _cap(s: str) -> str:
    return (s[:1].upper() + s[1:]) if s else s


def _genitive(word: str) -> str:
    """«любителей X» — родительный падеж. Для нашего словаря жанров."""
    w = _strip(word)
    if not w or w in _NO_INFL:
        return w
    if w in GENRE_RU:
        return GENRE_RU[w]
    last = w[-1]
    if last in "ая":
        # «рок» не подходит, а вот «металла» — да: убираем а/я, добавляем ы/и
        base = w[:-1]
        if base.endswith("и") or base.endswith("ия") or base.endswith("ь"):
            return base + "и"
        return base + "ы"
    if last == "ь":
        return w[:-1] + "и"
    if last in "ыи":
        return w + "ов"
    if last in "й":
        return w + "я"
    return w + "а"


def _describe(db, tracks: list[Any]) -> dict[str, Any]:
    """Доминирующие признаки плейлиста: жанры, настроения, энергия, темп."""
    genres: Counter = Counter()
    moods: Counter = Counter()
    energies: list[float] = []
    tempos: list[float] = []
    tids = [str(getattr(t, "id", "") or (t.get("id") if isinstance(t, dict) else "")) for t in (tracks or [])]
    tids = [t for t in tids if t]
    feats: dict[str, Any] = {}
    if tids:
        try:
            from app.db.models import TrackFeatures as _TF

            feats = {str(f.track_id): f for f in
                     db.query(_TF).filter(_TF.track_id.in_(tids)).all()}
        except Exception:
            feats = {}
    for t in (tracks or []):
        g = getattr(t, "genre", None) or (t.get("genre") if isinstance(t, dict) else None)
        if g:
            genres[_strip(g)] += 1
        tid = str(getattr(t, "id", "") or (t.get("id") if isinstance(t, dict) else ""))
        f = feats.get(tid)
        if f is None:
            continue
        try:
            for m in (f.mood_labels or [])[:3]:
                if m:
                    moods[_strip(m)] += 1
        except Exception:
            pass
        try:
            if f.energy is not None:
                energies.append(float(f.energy))
        except (TypeError, ValueError):
            pass
        try:
            if f.tempo_bpm:
                tempos.append(float(f.tempo_bpm))
        except (TypeError, ValueError):
            pass
    return {
        "genres": [g for g, _ in genres.most_common(3)],
        "moods": [m for m, _ in moods.most_common(2)],
        "mean_energy": (sum(energies) / len(energies)) if energies else None,
        "mean_tempo": (sum(tempos) / len(tempos)) if tempos else None,
        "n_genres": len(genres),
    }


def _time_part(hour: int | None) -> str:
    if hour is None:
        return ""
    if 5 <= hour < 12:
        return "Утренний"
    if 12 <= hour < 18:
        return "Дневной"
    if 18 <= hour < 23:
        return "Вечерний"
    return "Ночной"


def make_name(desc: dict[str, Any], query: str | None = None, hour: int | None = None) -> str:
    """Название плейлиста по доминирующим признакам. Детерминированно.

    Формат сознательно составной («Ночной микс · мечтательное»), а не
    прилагательное + прилагательное: у настроений разный род
    («спокойное» — средний, «ночной» — мужской), и согласование по роду
    требовало бы полноценной морфологии. Составной формат читается как теги
    и всегда грамматичен.
    """
    genres = [g for g in (desc.get("genres") or []) if g and g not in ("unknown", "none")]
    moods = [m for m in (desc.get("moods") or []) if m]
    mood_ru = [MOOD_RU.get(m, m) for m in moods]
    energy = desc.get("mean_energy")
    time_part = _time_part(hour)

    if genres and len(genres) >= 2:
        g1, g2 = _cap(genres[0]), _genitive(genres[1])
        # Тавтология: если после нормализации жанры совпали — не повторяемся.
        if _strip(genres[0]) == _strip(genres[1]):
            return f"Для любителей {g1}"
        if energy is not None and energy > 0.62:
            return f"Для любителей {g1} и {g2}"
        return f"{g1} и {g2}"

    if genres and mood_ru:
        return f"{time_part} микс · {_cap(genres[0])}".strip().capitalize() or "KumaFlow Mix"
    if genres:
        return f"{time_part} микс · {_cap(genres[0])}".strip().capitalize() or "KumaFlow Mix"
    if mood_ru:
        tail = " · " + _cap(mood_ru[0])
        return f"{time_part} микс{tail}".strip() if time_part else f"Микс{tail}"
    if query:
        q = str(query).strip()
        return f"Микс: {q[:40]}" if q else "KumaFlow Mix"
    return "KumaFlow Mix"


def make_comment(desc: dict[str, Any], query: str | None = None, mode: str = "") -> str:
    """Пояснение «почему такой микс» — то, что раньше молча пропадало."""
    parts: list[str] = []
    q = str(query or "").strip()
    if q:
        parts.append(f"по запросу «{q[:80]}»")
    genres = [g for g in (desc.get("genres") or []) if g and g not in ("unknown", "none")]
    if genres:
        parts.append("жанры: " + ", ".join(genres[:3]))
    moods = [m for m in (desc.get("moods") or []) if m]
    if moods:
        parts.append("настроение: " + ", ".join(MOOD_RU.get(m, m) for m in moods[:2]))
    energy = desc.get("mean_energy")
    if energy is not None:
        parts.append(f"энергия {'высокая' if energy > 0.62 else 'низкая' if energy < 0.4 else 'средняя'}")
    tempo = desc.get("mean_tempo")
    if tempo:
        parts.append(f"темп ~{int(round(tempo))} bpm")
    if mode:
        parts.append(f"режим: {mode}")
    if not parts:
        return ""
    return "Подобрано " + "; ".join(parts) + "."
