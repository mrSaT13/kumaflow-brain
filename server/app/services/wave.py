"""Аддитивная «Моя волна» — серверный порт мобильного MLWaveService.

Мобилу не трогаем: клиент присылает маленькую дельту (что уже в очереди,
что играет сейчас, свежие события, кусок оценок), сервер докладывает
следующие N треков тем же скорингом, что TrackScorer в мобиле.

Веса как в мобиле (_getWeights): мало лайков (<50) — больше жанра и
коллаборативки, много — больше аудио и поведения. Плюс контекст
(activity/mood/час), behaviorBonus по recent_events, diversityPenalty,
random*0.12.
"""
from __future__ import annotations

import math
import random
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import func


def _tun_all(db, user_id: str | None = None) -> dict:
    """Эффективный rec_tuning для юзера (дефолты <- глобал <- per-user).

    Один PK-запрос. Любая ошибка (нет таблицы, битая строка) -> {}:
    дальше везде дефолты-константы, поведение 1в1 как раньше.
    """
    try:
        from app.services import rec_tuning as _rt

        return _rt.get_all(db, user_id) or {}
    except Exception:
        return {}


def _tun(tun: dict, group: str, key: str, default):
    try:
        v = (tun.get(group) or {}).get(key, default)
        return default if v is None else v
    except Exception:
        return default


def _tun_float(tun: dict, group: str, key: str, default: float) -> float:
    try:
        return float(_tun(tun, group, key, default))
    except (TypeError, ValueError):
        return float(default)


def _tun_int(tun: dict, group: str, key: str, default: int) -> int:
    try:
        return int(float(_tun(tun, group, key, default)))
    except (TypeError, ValueError):
        return int(default)


def _tun_list(tun: dict, group: str, key: str,
              default: list, n: int) -> list:
    """Список нужной длины, иначе дефолт (защита от битой строки в БД)."""
    try:
        v = (tun.get(group) or {}).get(key)
        if isinstance(v, (list, tuple)) and len(v) >= n:
            return [float(x) for x in list(v)[:n]]
    except Exception:
        pass
    return [float(x) for x in list(default)[:n]]


def _weights(total_likes: int, tun: dict | None = None) -> dict[str, float]:
    cold = {'audio': 0.20, 'genre': 0.30, 'artist': 0.10,
            'behavior': 0.10, 'collab': 0.25, 'novelty': 0.05}
    warm = {'audio': 0.40, 'genre': 0.20, 'artist': 0.10,
            'behavior': 0.20, 'collab': 0.05, 'novelty': 0.05}
    tun = tun or {}
    th = _tun_int(tun, 'character', 'likes_threshold', 50)
    name = 'weights_cold' if total_likes < th else 'weights_warm'
    pick = cold if total_likes < th else warm
    try:
        stored = (tun.get('character') or {}).get(name)
        if isinstance(stored, dict) and all(k in stored for k in pick):
            return {k: float(stored[k]) for k in pick}
    except Exception:
        pass
    return dict(pick)


# Канонический словарь настроений. Три словаря в одном месте, потому что
# раньше они не совпадали и волна ломалась:
#   * пилюля в UI        -> «спокойное»  (род. падеж, как в подписи)
#   * код пилюли плеера  -> 'calm'
#   * муд-лейбл librosa   -> 'спокойный'  (TrackFeatures.mood_labels)
# Старый код сравнивал код пилюли с муд-лейблом напрямую, поэтому
# жёсткий mood-фильтр не срабатывал НИ РАЗУ: анализированные треки
# выкидывались из пула целиком, и в «Спокойное» доезжало всё, что без
# анализа. Любая форма -> один канонический код, дальше сравнения честные.
MOOD_ALIASES: dict[str, str] = {
    # пилюли UI (род. падеж)
    'бодрое': 'energetic', 'энергичное': 'energetic', 'энергичный': 'energetic',
    'весёлое': 'happy', 'веселое': 'happy', 'счастливое': 'happy',
    'спокойное': 'calm', 'спокойный': 'calm',
    'расслабленное': 'relaxed', 'расслабленный': 'relaxed',
    'мирное': 'calm', 'мирный': 'calm',
    'грустное': 'sad', 'грустный': 'sad', 'печальное': 'sad',
    'меланхоличное': 'melancholic', 'меланхоличный': 'melancholic',
    'тёмное': 'dark', 'темное': 'dark', 'тёмный': 'dark', 'темный': 'dark',
    'агрессивное': 'aggressive', 'агрессивный': 'aggressive',
    'боевое': 'aggressive', 'боевой': 'aggressive',
    'танцевальное': 'dance', 'танцевальный': 'dance',
    'мечтательное': 'dreamy', 'мечтательный': 'dreamy',
    'тёплое': 'warm', 'теплое': 'warm', 'тёплый': 'warm', 'теплый': 'warm',
    'тревожное': 'tense', 'тревожный': 'tense',
    'романтичное': 'romantic', 'романтичный': 'romantic',
    'эпичное': 'epic', 'эпичный': 'epic',
    # синонимы кодов, которые уже могли прийти из веба/настроек
    'relaxed': 'relaxed', 'peaceful': 'calm', 'excited': 'energetic',
    'upbeat': 'happy', 'angry': 'aggressive', 'dreamy': 'dreamy',
    'warm': 'warm', 'dance': 'dance', 'happy': 'happy', 'calm': 'calm',
    'sad': 'sad', 'dark': 'dark', 'energetic': 'energetic', 'tense': 'tense',
    'romantic': 'romantic', 'epic': 'epic', 'melancholic': 'melancholic',
    'chill': 'chill',
    # ВАЖНО: каждый канонический код обязан быть тут identity-маппингом.
    # Раньше коды не нормализовались вообще (norm_mood возвращал «как есть»),
    # и это работало случайно: 'aggressive' -> 'aggressive'. Со строгим
    # нормализатором без этой строки пилюля плеера 'aggressive' перестала бы
    # узнаваться — пресет не находился, фильтр выключался.
    'aggressive': 'aggressive',
}

# Подстрочки -> канонический код. Нужны потому, что слова настроения в БД
# приходят из ДВУХ источников: librosa (9 фиксированных слов) и LLM по тексту
# песни (lyrics_ai просит «3-5 short mood words in russian» — то есть любые
# слова). Раньше незнакомое слово молча превращалось в себя же, пресет не
# находился, а хард-фильтр вырождался в «оставь треки с этим словом» — и
# откатывался целиком. Итог: пилюля «боевой» работала как «настроение не
# выбрано». Порядок важен: первое совпадение выигрывает, поэтому специфичные
# подстроки стоят выше общих.
MOOD_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # тревога/напряжение — раньше sad, но «надрыв» и «тревога» это arousal
    ('tense', ('тревож', 'тревог', 'напряж', 'нервн', 'насторож', 'тревожн',
               'надрывн', 'гнетущ', 'изменчив', 'беспокой',
               'anxious', 'anxiety', 'tense', 'nervous', 'worry', 'worried',
               'stress', 'напряжен')),
    # бой/агрессия
    ('aggressive', ('агрессив', 'боев', 'боит', 'ярост', 'яро', 'злой', 'злоб',
                    'злост', 'свиреп', 'напор', 'дерзк', 'вызов', 'хищн',
                    'rage', 'angry', 'furious', 'hostile', 'combat', 'fight',
                    'war', 'military', 'attack', 'heavy', 'hardcore',
                    'hard rock', 'heavy metal', 'metal', 'punk', 'рок',
                    'хип-хоп', 'hip-hop', 'hiphop', 'рэп', 'rap', 'гнев')),
    # грусть
    ('sad', ('груст', 'печал', 'тоск', 'меланхол', 'слезив', 'плак', 'страдан',
             'одинок', 'депресс', 'подавлен', 'уныл', 'безнадёж', 'безнадеж',
             'sad', 'melanchol', 'sorrow', 'grief', 'lonely')),
    # тьма/мрачность
    ('dark', ('тёмн', 'темн', 'мрачн', 'мрач', 'ночн', 'непрогляд',
              'dark', 'somber', 'gothic', 'гроув')),
    # эпик
    ('epic', ('эпик', 'эпичн', 'монумент', 'торжеств', 'героик', 'масштабн',
              'epic', 'grand', 'grandiose', 'triumph')),
    # мечтательность
    ('dreamy', ('мечтат', 'призрач', 'воздушн', 'невесом', 'облачн', 'речной',
                'dream', 'dreamy', 'airy', 'ethereal')),
    # тепло/нежность — раньше уходило в calm, но «нежный» это warm
    ('warm', ('тёпл', 'тепл', 'уютн', 'ласк', 'нежн', 'золот', 'ностальг',
              'душевн', 'светл', 'warm', 'nostalg', 'tender', 'cozy', 'golden',
              'уют')),
    # спокойствие
    ('calm', ('спокой', 'умиротвор', 'безмятеж', 'медитац', 'шёпот', 'шепот',
              'плавн', 'размерен', 'тих', 'покой', 'chillout', 'chill out',
              'calm', 'peace', 'relax', 'meditat', 'ambient', 'serene',
              'mellow', 'quiet')),
    # расслабленность
    ('relaxed', ('расслаб', 'ленив', 'неспешн', 'lazy', 'laid-back')),
    # танец
    ('dance', ('танц', 'клубн', 'диско', 'свинг', 'дэнс',
               'dance', 'club', 'disco', 'groov', 'groove', 'bounce', 'swing')),
    # счастье
    ('happy', ('счастл', 'весел', 'весёл', 'радост', 'улыб', 'оптимиз',
               'восторг', 'восторжен', 'весель',
               'happy', 'joy', 'cheer', 'playful', 'uplift', 'bliss', 'upbeat',
               'sunny', 'bright')),
    # энергия
    ('energetic', ('энергич', 'бодр', 'мощн', 'мощ', 'драйв', 'драйвов',
                   'быстр', 'стремит', 'заводн', 'зажигат', 'активн', 'жизнен',
                   'energetic', 'drive', 'driving', 'pump', 'powerful',
                   'upbeat', 'fast')),
    # романтика
    ('romantic', ('романт', 'влюб', 'поцелуй', 'обним', 'чувств',
                  'romantic', 'love', 'tender')),
)

# Канонический код -> русская подпись для пилюли (каталог /api/wave/options).
MOOD_LABELS: dict[str, str] = {
    'energetic': 'Бодрое',
    'happy': 'Весёлое',
    'calm': 'Спокойное',
    'relaxed': 'Расслабленное',
    'chill': 'Chill',
    'sad': 'Грустное',
    'melancholic': 'Меланхоличное',
    'dark': 'Тёмное',
    'aggressive': 'Боевое',
    'dance': 'Танцевальное',
    'dreamy': 'Мечтательное',
    'warm': 'Тёплое',
    'tense': 'Тревожное',
    'romantic': 'Романтичное',
    'epic': 'Эпичное',
}

# Словарь, который LLM обязан выбирать (lyrics_ai). Если LLM выдаёт слово
# вне списка — оно всё равно нормализуется таблицей выше, но новые слова лучше
# не плодить: пилюли в плеере строятся по словарю БД, и каждый выдуманный
# лейбл превращается в отдельную пилюлю.
MOOD_VOCAB: tuple[str, ...] = (
    'энергичный', 'спокойный', 'танцевальный', 'счастливый', 'меланхоличный',
    'агрессивный', 'мечтательный', 'тёплый', 'тёмный', 'тревожный',
    'романтичный', 'эпичный',
)
# Пресеты «Моя волна». Ключ — канонический код (см. norm_mood).
# 'moods' — семейство муд-лейблов: один и тот же набор и для ctx-бонуса,
# и для жёсткого mood-фильтра (раньше фильтр брал только 1-й лейбл трека
# и сравнивал его с кодом пилюли — то есть не срабатывал никогда).
MOOD_PRESETS: dict[str, dict] = {
    'energetic': {'moods': {'energetic', 'excited', 'happy', 'dance'},
                  'energy_min': 0.6, 'tempo_min': 110, 'valence_min': 0.4},
    'happy': {'moods': {'happy', 'upbeat', 'energetic', 'dance'},
              'valence_min': 0.55, 'dance_min': 0.5},
    'calm': {'moods': {'calm', 'relaxed', 'peaceful', 'dreamy', 'warm'},
             'energy_max': 0.45, 'arousal_max': 0.45, 'tempo_max': 100},
    'sad': {'moods': {'sad', 'melancholic', 'dark'},
            'valence_max': 0.45},
    'dark': {'moods': {'dark', 'melancholic'},
             'valence_max': 0.45},
    'aggressive': {'moods': {'aggressive', 'energetic', 'dark'},
                   'energy_min': 0.65, 'tempo_min': 120,
                   # «Боевое» ≠ «бодрое»: быстрый МАЖОРНЫЙ EDM (valence ~0.75)
                   # проходил пилюлю наравне с Bodies, потому что семейство
                   # лейблов включает «энергичный», а это ровно energy.
                   # Теперь у пилюли есть и «мрачно»: valence низкий, arousal
                   # высокий. Мажорное и спокойное — мимо.
                   'valence_max': 0.55, 'arousal_min': 0.7},
    'chill': {'moods': {'calm', 'relaxed', 'dreamy'}, 'energy_max': 0.45,
              'tempo_max': 100},
    'relaxed': {'moods': {'calm', 'relaxed', 'peaceful', 'dreamy'},
                'energy_max': 0.45, 'tempo_max': 100},
    # --- пресеты, которых не было: каждый лейбл анализатора обязан иметь
    # пресет, иначе пилюля «работает», но фильтр молча выключен. ---
    'dance': {'moods': {'dance', 'energetic', 'happy'},
              'dance_min': 0.45, 'energy_min': 0.45, 'tempo_min': 95},
    'dreamy': {'moods': {'dreamy', 'calm', 'warm'},
               'energy_max': 0.5, 'arousal_max': 0.55, 'tempo_max': 115},
    'melancholic': {'moods': {'melancholic', 'sad', 'dark'},
                    'valence_max': 0.45, 'energy_max': 0.6},
    'warm': {'moods': {'warm', 'happy', 'romantic'},
             'valence_min': 0.45, 'energy_max': 0.7},
    'tense': {'moods': {'tense', 'sad', 'dark'},
              'arousal_min': 0.5, 'energy_min': 0.45},
    'romantic': {'moods': {'romantic', 'warm', 'calm'},
                 'valence_min': 0.45, 'energy_max': 0.75},
    'epic': {'moods': {'epic', 'energetic', 'dark'},
             'energy_min': 0.55, 'tempo_min': 100},
}
# --- Числовое муд-пространство (этап 2) ----------------------------------
# Пилюля — это ТОЧКА в 4-мерном пространстве звука + радиус вокруг неё.
# Оси: valence (настроение), energy (энергия), arousal (возбуждение),
# dance (танцевальность). Все 0..1 и уже лежат в TrackFeatures — отдельный
# анализ не нужен.
#
# Раньше настроение было строкой («агрессивный»), и из этого следовало:
#   * «счастливое» и «бодрое» — почти синонимы (обе про valence/energy);
#   * градиент морфа был БИНАРНЫМ: трек либо «стартовое настроение»,
#     либо «целевое» — трека «наполовину грустный, наполовину
#     поднимающийся» не существовало вовсе;
#   * слова от LLM — это про СЛОВА песни («боевая песня о любви»), а не
#     про звук. Теперь звук (вектор) — главный свидетель, слово — нет.
MOOD_AXES: tuple[str, ...] = ('valence', 'energy', 'arousal', 'dance')
MOOD_AXIS_W: dict[str, float] = {'valence': 1.0, 'energy': 0.95,
                                 'arousal': 0.85, 'dance': 0.6}
# Радиус пилюли: взвешенное RMS-расстояние от центра, дальше — «не то».
MOOD_RADIUS = 0.30
# Центры пилюль в муд-пространстве. Расставлены вручную по тем формулам,
# которыми анализатор считает лейблы (audio_analysis.mood_from_features),
# чтобы «Боевое» означало звук, а не слово из текста песни.
MOOD_TARGETS: dict[str, dict[str, float]] = {
    'energetic': {'valence': 0.58, 'energy': 0.80, 'arousal': 0.72, 'dance': 0.58},
    'happy': {'valence': 0.78, 'energy': 0.68, 'arousal': 0.62, 'dance': 0.68},
    'calm': {'valence': 0.50, 'energy': 0.24, 'arousal': 0.20, 'dance': 0.30},
    'relaxed': {'valence': 0.56, 'energy': 0.18, 'arousal': 0.14, 'dance': 0.24},
    'chill': {'valence': 0.50, 'energy': 0.26, 'arousal': 0.22, 'dance': 0.38},
    'sad': {'valence': 0.16, 'energy': 0.30, 'arousal': 0.28, 'dance': 0.24},
    'melancholic': {'valence': 0.26, 'energy': 0.36, 'arousal': 0.30, 'dance': 0.24},
    'dark': {'valence': 0.20, 'energy': 0.52, 'arousal': 0.44, 'dance': 0.30},
    'aggressive': {'valence': 0.30, 'energy': 0.88, 'arousal': 0.88, 'dance': 0.55},
    'dance': {'valence': 0.64, 'energy': 0.76, 'arousal': 0.70, 'dance': 0.85},
    'dreamy': {'valence': 0.46, 'energy': 0.28, 'arousal': 0.24, 'dance': 0.30},
    'warm': {'valence': 0.66, 'energy': 0.44, 'arousal': 0.40, 'dance': 0.40},
    'tense': {'valence': 0.28, 'energy': 0.58, 'arousal': 0.76, 'dance': 0.34},
    'romantic': {'valence': 0.64, 'energy': 0.40, 'arousal': 0.40, 'dance': 0.40},
    'epic': {'valence': 0.46, 'energy': 0.76, 'arousal': 0.70, 'dance': 0.42},
}

# Занятие: мягкие бонусы (не жёсткие фильтры — очередь не должна пустеть).
# Ключи-коды — контракт с плеером (GET /api/wave/options отдаёт каталог).
# Русские пилюли старых клиентов — алиасы (ACTIVITY_ALIASES).
ACTIVITY_PRESETS: dict[str, dict] = {
    'wakeup': {'energy_min': 0.5, 'tempo_min': 100, 'valence_min': 0.4},
    'commute': {'energy_min': 0.6, 'tempo_min': 110},
    'work': {'energy_min': 0.3, 'energy_max': 0.6, 'tempo_max': 120},
    'workout': {'energy_min': 0.65, 'tempo_min': 120},
    'sleep': {'energy_max': 0.35},
    'study': {'energy_min': 0.3, 'energy_max': 0.6, 'tempo_max': 120},
    'party': {'energy_min': 0.7, 'tempo_min': 115, 'dance_min': 0.5},
    'walk': {'energy_min': 0.4, 'energy_max': 0.7, 'valence_min': 0.4},
    'rest': {'energy_max': 0.4},
}
ACTIVITY_ALIASES: dict[str, str] = {
    'просыпаюсь': 'wakeup',
    'в дороге': 'commute',
    'работаю': 'work',
    'тренируюсь': 'workout',
    'тренировка': 'workout',
    'засыпаю': 'sleep',
}
# Занятия, где пилюля — это не «настроение», а «темп»: мягкого ctx-бонуса
# (+0.10) тут недостаточно, потому что энергия входит в скор с весом audio
# 0.4 и спокойный трек просто не дотягивал до топа. Для них — жёсткий фильтр
# по энергии/темпу, с тем же откатом, что у mood-фильтра.
# Сознательно только «успокаивающие»: work/study остаются мягкими, иначе
# фон для работы превращается в запрет.
ACTIVITY_HARD: dict[str, dict] = {
    'sleep': {'energy_max': 0.35, 'tempo_max': 95},
    'rest': {'energy_max': 0.4, 'tempo_max': 100},
    'workout': {'energy_min': 0.65, 'tempo_min': 120},
}
# Жанр -> муд для треков без анализа (фолбек, пока нет TrackFeatures).
# Точный звук всегда побеждает: как только трек проанализирован, решает аудио.
# Таблица — только первый рубеж. Второй рубеж (важнее): муд жанра,
# выведенный из ДАННЫХ — муд большинства проанализированных треков этого
# жанра (см. _genre_mood). Без него треки жанра, которого тут нет, проходили
# ЛЮБУЮ пилюлю: rap/hip-hop/pop/indie/lo-fi в таблицу не входили, и в
# «Боевое» попадал лоу-фай рэп.
GENRE_MOOD_FALLBACK: dict[str, str] = {
    'rock': 'aggressive', 'metal': 'aggressive', 'punk': 'aggressive',
    'hardcore': 'aggressive', 'hard rock': 'aggressive',
    'heavy metal': 'aggressive', 'alternative metal': 'aggressive',
    'ambient': 'calm', 'classical': 'calm', 'jazz': 'calm',
    'sleep': 'calm', 'lullaby': 'calm', 'meditation': 'calm',
    'dance': 'energetic', 'electronic': 'energetic', 'techno': 'energetic',
    'house': 'energetic', 'trance': 'energetic',
    'rap': 'aggressive', 'hip-hop': 'aggressive', 'hiphop': 'aggressive',
    'trap': 'aggressive', 'drill': 'aggressive', 'grime': 'aggressive',
    'punk rock': 'aggressive', 'industrial': 'aggressive', 'thrash': 'aggressive',
    'pop': 'happy', 'k-pop': 'happy', 'synthpop': 'energetic',
    'funk': 'energetic', 'disco': 'dance', 'boom bap': 'energetic',
    'r&b': 'calm', 'soul': 'warm', 'neo-soul': 'warm', 'blues': 'sad',
    'indie': 'dreamy', 'indie rock': 'dreamy', 'shoegaze': 'dreamy',
    'lo-fi': 'calm', 'lofi': 'calm', 'chillhop': 'calm', 'chillhop beat': 'calm',
    'downtempo': 'calm', 'trip-hop': 'calm', 'synthwave': 'dreamy',
    'drum and bass': 'energetic', 'drum and bass/jungle': 'energetic',
    'jungle': 'energetic', 'dubstep': 'aggressive', 'garage': 'dance',
    'drum and bass': 'energetic', 'hardstyle': 'aggressive', 'metalcore': 'aggressive',
    'folk': 'calm', 'acoustic': 'calm', 'singer-songwriter': 'warm',
    'country': 'warm', 'bluegrass': 'warm', 'gospel': 'happy',
    'orchestral': 'epic', 'cinematic': 'epic', 'soundtrack': 'dreamy',
    'score': 'epic', 'classical crossover': 'epic',
    'latin': 'dance', 'reggaeton': 'dance', 'salsa': 'dance',
    'afrobeat': 'energetic', 'afrobeats': 'energetic', 'reggae': 'calm',
    'ska': 'energetic', 'punk': 'aggressive', 'emo': 'sad', 'post-punk': 'dark',
    'goth': 'dark', 'shoegaze': 'dreamy', 'trip-hop': 'calm',
    'breakbeat': 'energetic', 'glitch': 'tense', 'industrial': 'aggressive',
}


def _genre_mood(genre: str | None, auto: dict[str, str] | None = None) -> str | None:
    """Муд по жанру для unknown-треков.

    Порядок: таблица -> выведенный из данных (auto) -> None (= пропуск).
    None означает «жанр неизвестен»: трек проходит фильтр, но в скоринге
    получает штраф (см. score_candidates) — не наравне с тем, что мы знаем.
    """
    try:
        g = (genre or '').strip().lower()
    except Exception:
        return None
    if not g:
        return None
    for key, mood in GENRE_MOOD_FALLBACK.items():
        if key in g:
            return mood
    if auto:
        # Подстрочное совпадение: жанр в БД может быть 'Hip-Hop/Rap'.
        for key, mood in auto.items():
            if key and key in g:
                return mood
        if g in auto:
            return auto[g]
    return None
# Каталог для плеера (код — то, что плеер шлёт обратно в settings.activity).
ACTIVITY_CATALOG: list[dict] = [
    {'code': 'wakeup', 'label': 'Просыпаюсь', 'hint': 'бодро, но мягко'},
    {'code': 'commute', 'label': 'В дороге', 'hint': 'энергия и темп'},
    {'code': 'work', 'label': 'Работаю', 'hint': 'ровный фон'},
    {'code': 'study', 'label': 'Учёба / фокус', 'hint': 'не отвлекает'},
    {'code': 'workout', 'label': 'Тренировка', 'hint': 'темп и драйв'},
    {'code': 'walk', 'label': 'Прогулка', 'hint': 'светлое, в движении'},
    {'code': 'party', 'label': 'Вечеринка', 'hint': 'максимум движа'},
    {'code': 'rest', 'label': 'Отдых', 'hint': 'спокойное'},
    {'code': 'sleep', 'label': 'Засыпаю', 'hint': 'тихо и медленно'},
]
CHARACTERISTIC_CATALOG: list[dict] = [
    {'code': 'favorite', 'label': 'Любимое', 'hint': 'проверенное'},
    {'code': 'unfamiliar', 'label': 'Незнакомое', 'hint': 'открытия'},
    {'code': 'popular', 'label': 'Популярное', 'hint': 'у всех на слуху'},
]
LANGUAGE_CATALOG: list[dict] = [
    {'code': 'ru', 'label': 'Русский'},
    {'code': 'foreign', 'label': 'Иностранный'},
    {'code': 'instrumental', 'label': 'Без слов'},
]


def _norm_activity(v: str | None) -> str | None:
    """Код занятия: алиасы старых пилюль -> канон. Неизвестное — как есть
    (пресета нет = без бонусов, очередь не пустеет)."""
    s = (v or '').strip().lower()
    if not s:
        return None
    return ACTIVITY_ALIASES.get(s, s)


def norm_mood(v: Any) -> str | None:
    """Любая форма настроения -> канонический код ('спокойное'/'спокойный'/'calm').

    Строгий: неизвестное -> None (а НЕ «как есть»). Раньше неизвестное слово
    возвращалось само по себе, и это ломало всё:
      * пресет не находился -> ctx-бонусов за настроение ноль;
      * хард-фильтр строил _want={слово} и оставлял только треки с этим
        словом -> пул схлопывался -> фильтр откатывался целиком.
    Итог: пилюля «боевой» (слово из LLM, не из канона) работала как
    «настроение не выбрано». Теперь неизвестное -> None -> фильтр не
    включается, но и не ломается: волна остаётся рабочей.

    Порядок: точное совпадение -> таблица подстрок (MOOD_KEYWORDS).
    """
    s = str(v or '').strip().lower().replace('ё', 'е')
    if not s:
        return None
    c = MOOD_ALIASES.get(s)
    if c:
        return c
    for code, keys in MOOD_KEYWORDS:
        for k in keys:
            if k in s:
                return code
    return None


def norm_mood_raw(v: Any) -> str:
    """Сырое слово в нижнем регистре — для показа и для каталога."""
    return str(v or '').strip().lower().replace('ё', 'е')


def _norm_mood(v: str | None) -> str | None:
    """Историческое имя — код пилюли в канонический вид."""
    return norm_mood(v)


def _norm_language(v: str | None) -> str | None:
    s = (v or '').strip().lower()
    if not s:
        return None
    if s in ('ru', 'ru-ru', 'русский', 'russian', 'рус'):
        return 'ru'
    if s in ('foreign', 'en', 'eng', 'иностранный', 'английский', 'не русский'):
        return 'foreign'
    if s in ('instrumental', 'без слов', 'инструментал', 'no_lyrics', 'no lyrics'):
        return 'instrumental'
    return None


def _has_cyrillic(*parts: object) -> bool:
    """Есть ли кириллица в любом из кусков (метаданные трека)."""
    for p in parts:
        if not p:
            continue
        for c in str(p):
            if "\u0400" <= c <= "\u04ff":
                return True
    return False


# Немузыкальные треки: скиты/интерлюдии/интро-аутро — разговорные вставки
# альбомов, которые в волне раздражают. Тумблер — Автоматизация →
# wave_skip_non_music (дефолт True). Проверка только по названию:
# маркер в скобках («Song (skit)») или название целиком («Skit 3»).
NON_MUSIC_MARKERS = ("skit", "skits", "interlude", "interludes", "intro",
                     "outro", "prelude", "prologue", "epilogue")


def _is_non_music_track(title: Any) -> bool:
    """True — разговорный трек (скит/интерлюдия), в волну не берём."""
    import re as _re

    t = (title or "").strip()
    if not t:
        return False
    try:
        _m = _re.search(r"[\(\[]([^\)\]]+)[\)\]]", t)
        if _m and _re.search(r"\b(" + "|".join(NON_MUSIC_MARKERS) + r")\b",
                             _m.group(1).lower()):
            return True
        _head = _re.sub(r"[\(\[].*[\)\]]", "", t).strip().lower()
        _head = _re.sub(r"[#№]?\s*\d+\s*$", "", _head).strip()
        return _head in NON_MUSIC_MARKERS
    except Exception:
        return False


def _meta_ru_ids(db, ids: list) -> set:
    """Кириллический fast-path: треки БЕЗ текстов (нет в Lyrics), но с
    кириллицей в метаданных (артист/название/альбом) считаем русскоязычными.

    High-precision без сети. Обратное неверно: латиница НЕ означает foreign
    (русские группы с английскими названиями сплошь и рядом), поэтому
    не-кириллические unknown по-прежнему пропускаем (unknown = pass).
    Реально чинит фильтры 'foreign' (убирает русское без текстов) и
    'instrumental' (у кириллического трека точно есть слова). Фильтр 'ru'
    от иностранных без текстов лечится только скачанными текстами или
    language_strict (см. ниже)."""
    from app.db.models import Track as _Track

    out: set = set()
    uniq = list(dict.fromkeys(str(i) for i in (ids or []) if i))[:2000]
    for i in range(0, len(uniq), 500):
        try:
            rows = db.query(_Track).filter(_Track.id.in_(uniq[i:i + 500])).all()
        except Exception:
            break
        for t in rows or []:
            try:
                if _has_cyrillic(getattr(t, "artist_name", None),
                                getattr(t, "title", None),
                                getattr(t, "album_name", None)):
                    out.add(str(getattr(t, "id", "")))
            except Exception:
                continue
    out.discard("")
    return out


def _resolve_ids(db, ids: list[str]) -> list[str]:
    """uuid как есть, external_id (мобильный id) -> наш track_id. Не падает на PG."""
    from app.services.track_resolve import resolve_track_ids as _r

    return _r(db, ids)


def apply_delta(db, user_id: str, ratings_delta: list[dict] | None,
                recent_events: list[dict] | None) -> dict:
    """Применить дельту от клиента: оценки + события. Отдельный синк не нужен."""
    from app.services import taste as _taste

    applied_r = applied_e = 0
    if ratings_delta:
        # Нормализуем к виду sync_from_mobile ratings (external_id или track_id)
        norm: list[dict] = []
        for r in ratings_delta:
            if not isinstance(r, dict):
                continue
            d = dict(r)
            tid = str(d.get('track_id') or '')
            if tid and d.get('external_id') is None:
                from app.services.track_resolve import get_track as _gt
                t = _gt(db, tid)
                if t is not None:
                    d['external_id'] = t.external_id
            norm.append(d)
        res = _taste.sync_from_mobile(db, user_id,
                                      {'ratings': norm, 'profile': {}, 'events': []})
        applied_r = int(res.get('ratings_seen', 0) or 0)
    if recent_events:
        norm_e: list[dict] = []
        for e in recent_events:
            if not isinstance(e, dict):
                continue
            tid = str(e.get('track_id') or '')
            if not tid:
                continue
            from app.services.track_resolve import get_track as _gt2
            _t = _gt2(db, tid)
            if _t is None:
                continue
            tid = str(_t.id)
            norm_e.append({'track_id': tid, 'action': e.get('action'),
                           'position_sec': e.get('position_sec')})
        if norm_e:
            from datetime import datetime as _dt

            from app.db.models import PlayHistory as _PH

            res = _taste.record_events(db, user_id, norm_e, limit=500)
            applied_e = int(res.get('stored', 0) or 0)
            # play-события — ещё и в историю (память мозга для GET history)
            for e in norm_e:
                if str(e.get('action') or '') == 'play':
                    db.add(_PH(user_id=user_id, track_id=str(e['track_id']),
                               played_at=_dt.utcnow()))
            db.commit()
    return {'ratings_applied': applied_r, 'events_applied': applied_e}


def select_seeds(db, user_id: str, characteristic: str | None = None,
                 limit: int = 5) -> list[str]:
    """Сиды как в мобиле: топ по скору + недавние + random."""
    from app.services import taste as _taste

    prof = _taste.user_profile(db, user_id, top_n=200)
    top = prof.get('top_tracks') or []
    if not top:
        return []
    tun = _tun_all(db, user_id)
    _suffix = {'favorite': 'favorite', 'unfamiliar': 'unfamiliar',
               'popular': 'popular'}.get(characteristic or '', 'default')
    _dflt = {'favorite': (0.7, 0.2), 'unfamiliar': (0.3, 0.2),
             'popular': (0.6, 0.3)}.get(characteristic or '', (0.5, 0.25))
    top_ratio = _tun_float(tun, 'playlists', f'seed_top_{_suffix}', _dflt[0])
    recent_ratio = _tun_float(tun, 'playlists', f'seed_recent_{_suffix}', _dflt[1])
    # round() в питоне — банковское округление (round(2.5) == 2), поэтому
    # «50 % топ» при limit=5 давало 2 сида вместо 3. Считаем по-человечески.
    top_n = int(math.floor(limit * top_ratio + 0.5))
    rec_n = int(math.floor(limit * recent_ratio + 0.5))
    out = [t['track_id'] for t in top[:top_n]]
    # Недавние: по last_played
    dated = [t for t in top if t.get('last_played')]
    dated.sort(key=lambda t: t['last_played'], reverse=True)
    for t in dated:
        if len(out) >= top_n + rec_n:
            break
        if t['track_id'] not in out:
            out.append(t['track_id'])
    rest = [t['track_id'] for t in top if t['track_id'] not in out]
    # Перемешивание — СЕЯННЫМ генератором (раньше глобальный random):
    # иначе центроид «похоже на то, что ты любишь» гулял между двумя
    # одинаковыми запросами, и одна и та же пилюля давала разную выдачу.
    random.Random(f"{user_id}:{characteristic or ''}:{limit}").shuffle(rest)
    out.extend(rest[:max(0, limit - len(out))])
    return out[:limit]


def session_drift(db, recent_events: list[dict] | None,
                  user_id: str | None = None) -> dict:
    """Порт мобильного MoodDriftDetector: сессия, а не вечность.

    recent_events — в хронологическом порядке (как шлёт клиент).
    - 3/5/7 скипов подряд в хвосте -> mild/moderate/strong (энергия/темп вниз).
    - 3+ скипа жанра -> временный бан жанра НА ЭТОТ ЗАПРОС (в БД не пишем).
    - скипнутые треки -> исключить из кандидатов НА ЭТОТ ЗАПРОС.
    Постоянные счётчики (3 скипа ever -> автодизлайк) не трогаем.
    """
    events = [e for e in (recent_events or []) if isinstance(e, dict)]
    # Пороги дрейфа из личного тюнинга (дефолт 3/5/7, энергия 0.1/0.2/0.3,
    # темп 10/20/30). Битые значения -> дефолт, сортировка на всякий случай.
    tun = _tun_all(db, user_id)
    _dc = [int(x) for x in _tun_list(tun, 'skips', 'drift_counts', [3, 5, 7], 3)]
    _de = _tun_list(tun, 'skips', 'drift_energy', [0.1, 0.2, 0.3], 3)
    _dt = _tun_list(tun, 'skips', 'drift_tempo', [10, 20, 30], 3)
    _dc = sorted(_dc)
    # Хвостовые скипы подряд (позитив обнуляет серию — как logPositiveInteraction).
    trailing = 0
    for e in reversed(events):
        if str(e.get("action") or "") == "skip":
            trailing += 1
        else:
            break
    if trailing >= _dc[2]:
        severity, energy_shift, tempo_shift = "strong", -_de[2], -_dt[2]
    elif trailing >= _dc[1]:
        severity, energy_shift, tempo_shift = "moderate", -_de[1], -_dt[1]
    elif trailing >= _dc[0]:
        severity, energy_shift, tempo_shift = "mild", -_de[0], -_dt[0]
    else:
        severity, energy_shift, tempo_shift = None, 0.0, 0
    # Зеркало: хвост позитива подряд (like/replay/complete/seek_back) —
    # разогрев волны (порт мобильного разогрева: скипы остужают, любовь греет).
    # Со скипами взаимоисключающе по построению хвоста.
    _POS_ACTS = {"like", "replay", "complete", "seek_back"}
    warm_streak = 0
    for e in reversed(events):
        if str(e.get("action") or "") in _POS_ACTS:
            warm_streak += 1
        else:
            break
    if warm_streak >= _dc[2]:
        warmth, energy_shift, tempo_shift = "strong", _de[2], _dt[2]
    elif warm_streak >= _dc[1]:
        warmth, energy_shift, tempo_shift = "moderate", _de[1], _dt[1]
    elif warm_streak >= _dc[0]:
        warmth, energy_shift, tempo_shift = "mild", _de[0], _dt[0]
    else:
        warmth, warm_streak = None, 0

    skip_ids: list[str] = []
    genre_hits: Counter = Counter()
    if events:
        raws = [str(e.get("track_id") or "") for e in events]
        tids = _resolve_ids(db, raws)
        id_by_raw = dict(zip(raws, tids))
        seen: set[str] = set()
        for e in events:
            if str(e.get("action") or "") != "skip":
                continue
            tid = id_by_raw.get(str(e.get("track_id") or ""))
            if tid and tid not in seen:
                seen.add(tid)
                skip_ids.append(tid)
        if skip_ids:
            try:
                from app.db.models import Track as _T

                for t in db.query(_T).filter(_T.id.in_(skip_ids[:100])).all():
                    if t.genre:
                        genre_hits[str(t.genre).lower()] += 1
            except Exception:
                pass
    _ban_n = _tun_int(tun, 'skips', 'genre_ban_skips', 3)
    temp_banned = sorted([g for g, n in genre_hits.items() if n >= _ban_n])
    return {"severity": severity, "energy_shift": energy_shift,
            "tempo_shift": tempo_shift, "consecutive_skips": trailing,
            "warmth": warmth, "positive_streak": warm_streak,
            "temp_banned_genres": temp_banned, "skip_ids": skip_ids}


def _mood_of(feat) -> str | None:
    """Главный муд-лейбл трека в каноническом коде ('спокойный' -> 'calm').

    Именно ПЕРВЫЙ лейбл (как и было) — иначе порядок из set() плавал бы
    между вызовами и морфинг/штраф за повтор настроения считали бы разное.
    """
    try:
        labels = list(feat.mood_labels or [])
    except Exception:
        return None
    for m in labels:
        try:
            c = norm_mood(m)
        except Exception:
            continue
        if c:
            return c
    return None


def _mood_codes_of(feat) -> set[str]:
    """Все муд-лейблы трека в канонических кодах.

    Анализатор кладёт в mood_labels РУССКИЕ лейблы ('спокойный',
    'агрессивный'), пилюля плеера — англ. код ('calm'). Раньше эти словари
    сравнивались напрямую, и любое сравнение молча давало False.
    """
    out: set[str] = set()
    try:
        labels = list(feat.mood_labels or [])
    except Exception:
        return out
    for m in labels:
        try:
            c = norm_mood(m)
        except Exception:
            continue
        if c:
            out.add(c)
    return out


def mood_vector(feat) -> dict[str, float]:
    """Аудио-вектор трека по 4 осям (0..1). Нет анализа -> пустой dict.

    Оси: valence / energy / arousal / dance. Это ровно те числа, что
    анализатор уже кладёт в TrackFeatures, поэтому ничего нового
    считать не нужно.
    """
    out: dict[str, float] = {}
    if feat is None:
        return out
    for axis, attr in (('valence', 'valence'), ('energy', 'energy'),
                        ('arousal', 'arousal'), ('dance', 'danceability')):
        try:
            raw = getattr(feat, attr, None)
            val = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            val = None
        if val is None:
            continue
        out[axis] = min(max(val, 0.0), 1.0)
    return out


def mood_vec_dist(vec: dict[str, float], target: dict[str, float]) -> float:
    """Взвешенное RMS-расстояние между вектором трека и центром пилюли.

    Считаются только оси, которые есть и у вектора, и у цели: трек без
    arousal не должен штрафоваться за отсутствие координаты.
    """
    num = 0.0
    den = 0.0
    for axis, w in MOOD_AXIS_W.items():
        if axis not in vec or axis not in target:
            continue
        try:
            d = float(vec[axis]) - float(target[axis])
        except (TypeError, ValueError):
            continue
        num += w * d * d
        den += w
    if den <= 0:
        return 1.0
    return math.sqrt(num / den)


def mood_fit(vec: dict[str, float], target: dict[str, float],
             radius: float = MOOD_RADIUS) -> tuple[float, float]:
    """(близость, расстояние): 1 — в центре пилюли, 0 — на радиусе."""
    if not vec or not target:
        return 0.0, 1.0
    d = mood_vec_dist(vec, target)
    r = max(float(radius or MOOD_RADIUS), 1e-6)
    return max(0.0, 1.0 - d / r), d


def genre_vector_map(feats: dict[str, Any], genres: dict[str, str]
                      ) -> dict[str, dict[str, float]]:
    """Средний аудио-вектор жанра по проанализированным трекам пула.

    Это второй рубеж для треков БЕЗ анализа: своего вектора у них нет,
    а жанр внутри библиотеки достаточно однороден по звуку. Без этого
    трек, который ещё не проанализирован, вообще не мог соответствовать
    ни одной пилюле — и волна на непроанализированной библиотеке
    вырождалась в «всё, о чём мозг ничего не знает».
    """
    acc: dict[str, dict[str, float]] = {}
    cnt: dict[str, int] = {}
    for tid, f in (feats or {}).items():
        g = str((genres or {}).get(tid) or '').strip().lower()
        if not g or f is None:
            continue
        v = mood_vector(f)
        if not v:
            continue
        a = acc.setdefault(g, {})
        for k, val in v.items():
            a[k] = a.get(k, 0.0) + val
        cnt[g] = cnt.get(g, 0) + 1
    out: dict[str, dict[str, float]] = {}
    for g, a in acc.items():
        n = cnt.get(g, 0)
        if n <= 0:
            continue
        out[g] = {k: a[k] / n for k in a}
    return out


def mood_vectors_for(feats: dict[str, Any], genres: dict[str, str]
                      ) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """(векторы треков, векторы жанров).

    Трек без анализа получает вектор своего жанра: он НЕ исключается из
    волны, пока идёт анализ, но и не притворяется проверенным — это
    честная оценка «примерно так» с пониженной уверенностью.
    """
    gvecs = genre_vector_map(feats, genres)
    out: dict[str, dict[str, float]] = {}
    for tid, f in (feats or {}).items():
        v = mood_vector(f)
        if v:
            out[tid] = v
    for tid in (genres or {}):
        if tid in out:
            continue
        gv = gvecs.get(str((genres or {}).get(tid) or '').strip().lower())
        if gv:
            out[tid] = dict(gv)
    return out, gvecs


def _wave_vectors(feats: dict[str, Any], meta: dict[str, Any]
                  ) -> dict[str, dict[str, float]]:
    """Векторы муд-пространства для треков пула: свой анализ, иначе жанр."""
    genres: dict[str, str] = {}
    for tid, t in (meta or {}).items():
        try:
            genres[tid] = (getattr(t, 'genre', '') or '')
        except Exception:
            genres[tid] = ''
    vecs, _gvecs = mood_vectors_for(feats, genres)
    return vecs


def mood_filter_passes(feat, genre: str | None, want: set[str],
                       target: dict[str, float] | None = None,
                       radius: float = MOOD_RADIUS, limits: tuple = (),
                       gvecs: dict[str, dict[str, float]] | None = None,
                       genre_auto: dict[str, str] | None = None) -> bool:
    """Проходит ли трек пилюлю настроения.

    feat — TrackFeatures трека или None (анализа нет).
    genre — жанр трека (может быть пустым).
    want — семейство муд-лейблов пилюли; ПУСТОЕ = фильтр по настроению
    выключен (пилюли нет или её код неизвестен мозгу).
    target — центр пилюли в муд-пространстве; None = числовой части нет.
    limits — наборы рамок: (пресет настроения, жёсткое занятие).

    Правила:
      * трек без анализа решает жанр (таблица, затем муд, выведенный из
        данных); неизвестный жанр = пропуск — фильтр не знает, что сказать,
        и выкидывать «немые» треки нельзя: пока идёт анализ, они должны
        оставаться в игре;
      * у трека с анализом слово от LLM (про текст песни) не важно, если по
        ЗВУКУ он внутри пилюли: звук — главный свидетель;
      * рамки (энергия/темп/valence/dance/arousal) проверяются всегда, когда
        есть данные: coarse-фильтр, который не даёт «спокойному» треку
        пройти пилюлю «Боевое».
    """
    if not want and not target:
        return True
    if feat is None:
        if not want:
            return True
        gm = _genre_mood(genre, genre_auto)
        if gm is None:
            return True
        return gm in want
    if want and not (_mood_codes_of(feat) & want):
        # Звук важнее слов: трек, у которого слово чужое, но звук ровно в
        # пилюле, проходит.
        v = mood_vector(feat)
        if not v:
            v = (gvecs or {}).get(str(genre or '').strip().lower()) or {}
        if not (target and v and mood_vec_dist(v, target) <= radius):
            return False
    try:
        en = float(feat.energy) if feat.energy is not None else 0.5
    except (TypeError, ValueError):
        en = 0.5
    try:
        bpm = float(feat.tempo_bpm) if feat.tempo_bpm else 0.0
    except (TypeError, ValueError):
        bpm = 0.0
    try:
        va = float(feat.valence) if feat.valence is not None else None
    except (TypeError, ValueError):
        va = None
    try:
        da = float(feat.danceability) if feat.danceability is not None else None
    except (TypeError, ValueError):
        da = None
    try:
        ar = float(feat.arousal) if feat.arousal is not None else None
    except (TypeError, ValueError):
        ar = None
    for lim in (limits or ()):
        if not lim:
            continue
        if 'energy_min' in lim and en < lim['energy_min']:
            return False
        if 'energy_max' in lim and en > lim['energy_max']:
            return False
        if 'tempo_min' in lim and bpm and bpm < lim['tempo_min']:
            return False
        if 'tempo_max' in lim and bpm and bpm > lim['tempo_max']:
            return False
        if va is not None:
            if 'valence_min' in lim and va < lim['valence_min']:
                return False
            if 'valence_max' in lim and va > lim['valence_max']:
                return False
        if da is not None:
            if 'dance_min' in lim and da < lim['dance_min']:
                return False
            if 'dance_max' in lim and da > lim['dance_max']:
                return False
        if ar is not None:
            if 'arousal_min' in lim and ar < lim['arousal_min']:
                return False
            if 'arousal_max' in lim and ar > lim['arousal_max']:
                return False
    return True


def _clap_weight(tun: dict | None = None) -> float:
    """Вес CLAP-косинуса в волне. 0 = выключено → поведение 1в1 как раньше.

    Без анализа/эмбеддингов вклад и так 0 (карта пустая), но флаг позволяет
    откатить фичу из веба без деплоя. Дефолт из личного тюнинга (0.08):
    заметно, но не ломает баланс.
    """
    try:
        from app.services.automation import clap_audio_enabled as _flag

        if not _flag():
            return 0.0
    except Exception:
        pass
    return _tun_float(tun or {}, 'character', 'clap_weight', 0.08)


def _load_clap_map(db, ids: list[str] | None,
                   model: str = "clap_audio") -> dict:
    """track_id -> нормализованный np-вектор CLAP. Пусто = безопасный фолбек.

    Грузим только нужные id (пул скоринга + сиды + сессия, ~1200), батчами.
    Любая ошибка (нет таблицы, нет анализа, нет numpy) → {} и волна
    считается только по librosa, ничего не ломается.
    """
    if not ids:
        return {}
    try:
        import numpy as _np

        from app.db.models import TrackEmbedding as _TE
    except Exception:
        return {}
    try:
        uniq = list(dict.fromkeys(map(str, ids)))[:1500]
        out: dict = {}
        for i in range(0, len(uniq), 500):
            try:
                rows = db.query(_TE).filter(
                    _TE.model == model,
                    _TE.track_id.in_(uniq[i:i + 500])).all()
            except Exception:
                continue
            for r in rows:
                try:
                    v = _np.frombuffer(r.vector, dtype=_np.float32).astype(_np.float32)
                    n = float(_np.linalg.norm(v))
                    if n > 0:
                        out[str(r.track_id)] = v / n
                except Exception:
                    continue
        return out
    except Exception:
        return {}


def infer_auto_mood(db, user_id: str, session_ids: list[str] | None,
                    current_hour: int | None = None) -> str | None:
    """Автодетект настроения из центроида сессии (без пилюль).

    Берём energy/valence среднего по последним трекам сессии; маппим на
    канонические коды MOOD_PRESETS. Нет фичей/сессии → None (чистое
    авто-без-муда). Ручная пилюля всегда побеждает — см. wave_continue.
    """
    try:
        sids = [str(s) for s in (session_ids or []) if s][:10]
        if not sids:
            return None
        from app.db.models import TrackFeatures as _TF

        feats = {str(f.track_id): f for f in
                 db.query(_TF).filter(_TF.track_id.in_(sids)).all()}
        ens, vas = [], []
        for tid in sids:
            f = feats.get(tid)
            if f is None:
                continue
            try:
                if f.energy is not None:
                    ens.append(float(f.energy))
                if f.valence is not None:
                    vas.append(float(f.valence))
            except (TypeError, ValueError):
                continue
        if not ens:
            return None
        en = sum(ens) / len(ens)
        va = sum(vas) / len(vas) if vas else 0.5
        # Ночь — тянем к спокойному, утро — к бодрому (мягко).
        try:
            h = int(current_hour) if current_hour is not None else -1
        except (TypeError, ValueError):
            h = -1
        if va <= 0.38 and en < 0.55:
            return "sad"
        if en >= 0.62 and va >= 0.45:
            return "energetic"
        if en <= 0.42:
            return "calm"
        if va >= 0.6 and en >= 0.45:
            return "happy"
        if va <= 0.42:
            return "dark"
        if h >= 22 or (0 <= h <= 5):
            return "calm" if en < 0.55 else "dark"
        if 6 <= h <= 10:
            return "energetic" if en >= 0.5 else "calm"
        return "chill" if en < 0.5 else "energetic"
    except Exception:
        return None


def skip_risk(track, feat, pref_g: dict, cur_energy: float | None,
              drift: dict | None, fatigue: dict | None) -> float:
    """Эвристический P(skip<30с) 0..1. Без ML-модели — прозрачные правила.

    Признаки: скачок энергии/темпа от текущего, незнакомый жанр,
    загонянный артист, активный скип-стрик. Обученная логистическая модель
    (когда наберётся 5-10к событий) просто заменит эту функцию изнутри.
    """
    try:
        r = 0.0
        if feat is not None:
            try:
                en = float(feat.energy) if feat.energy is not None else 0.5
            except (TypeError, ValueError):
                en = 0.5
            try:
                bpm = float(feat.tempo_bpm) if feat.tempo_bpm else 0.0
            except (TypeError, ValueError):
                bpm = 0.0
            if cur_energy is not None:
                dj = abs(en - float(cur_energy))
                if dj > 0.45:
                    r += 0.35
                elif dj > 0.3:
                    r += 0.18
            if bpm and bpm > 150:
                r += 0.08
        g = (getattr(track, "genre", None) or "").strip().lower()
        if g and float(pref_g.get(g, 0.0)) <= 0.0:
            r += 0.10
        if fatigue and getattr(track, "artist_name", None) in fatigue:
            try:
                if int(fatigue[getattr(track, "artist_name")] or 0) > 10:
                    r += 0.20
            except (TypeError, ValueError):
                pass
        sev = (drift or {}).get("severity")
        if sev == "strong":
            r += 0.25
        elif sev == "moderate":
            r += 0.15
        elif sev == "mild":
            r += 0.08
        return min(max(r, 0.0), 1.0)
    except Exception:
        return 0.0


def _skip_prob(pipe, track, feat, pref_g: dict, cur_energy: float | None,
               drift: dict | None, fatigue: dict | None,
               agg_entry: dict | None, hour: int, dow: int) -> float:
    """P(skip<30с): обученная модель, при недоступности — эвристика.

    Сигнатура шире, чем у skip_risk: модели нужны агрегаты пары и время.
    Любая ошибка внутри — тихий фолбек на старые правила.
    """
    if pipe is not None:
        try:
            from app.services import skip_model as _sm

            try:
                en = float(feat.energy) if feat is not None and feat.energy is not None else 0.5
            except (TypeError, ValueError):
                en = 0.5
            try:
                va = float(feat.valence) if feat is not None and feat.valence is not None else 0.5
            except (TypeError, ValueError):
                va = 0.5
            try:
                da = float(feat.danceability) if feat is not None and feat.danceability is not None else 0.5
            except (TypeError, ValueError):
                da = 0.5
            try:
                bpm = float(feat.tempo_bpm) if feat is not None and feat.tempo_bpm else 0.0
            except (TypeError, ValueError):
                bpm = 0.0
            g = (getattr(track, "genre", None) or "").strip().lower()
            try:
                fam = float((pref_g or {}).get(g, 0.0)) if g else 0.0
            except (TypeError, ValueError):
                fam = 0.0
            try:
                an = getattr(track, "artist_name", None)
                _fc = int((fatigue or {}).get(an, 0) or 0) if an else 0
            except (TypeError, ValueError):
                _fc = 0
            a = agg_entry or {}
            try:
                sev = (drift or {}).get("severity")
                dl = 3 if sev == "strong" else (2 if sev == "moderate" else (1 if sev == "mild" else 0))
            except Exception:
                dl = 0
            vec = _sm.features_for(
                energy=en, valence=va, danceability=da, tempo_bpm=bpm,
                anchor_energy=cur_energy,
                unfamiliar_genre=bool(g and fam <= 0.0),
                artist_fatigue=min(_fc, 20) / 20.0,
                hour=hour, day_of_week=dow,
                plays=int(a.get("plays") or 0),
                early_skips=int(a.get("early_skips") or 0),
                completes=int(a.get("completes") or 0),
                drift_level=dl)
            p = _sm.predict_proba(pipe, vec)
            if p is not None:
                return p
        except Exception:
            pass
    return skip_risk(track, feat, pref_g, cur_energy, drift, fatigue)


def _context_overrides(act_code: str | None, mood_code: str | None,
                       jitter: float, mood_penalty: list,
                       novelty_extra: float) -> tuple:
    """Ужесточение характера под пилюлю (in-memory, в базу не пишем).

    Пилюля «Тренировка» на плеере иначе давала только жёсткий фильтр, а характер
    (разброс/удержание/новизна) оставался общим — контраст слабее, чем от
    кнопок-пресетов в тюнинге. Правило: только ужесточаем сохранённое,
    сохранённое строже — побеждает оно. Пилюля выключена — всё как было.
    Чистая функция — тестируется без БД.
    """
    try:
        act = act_code or ''
        m = mood_code or ''
        mp = list(mood_penalty)
        if act == 'workout' or m == 'aggressive':
            jitter = min(float(jitter), 0.04)
            mp = [max(float(a), b) for a, b in zip(mp, [0.10, 0.20, 0.30])]
            novelty_extra = min(float(novelty_extra), 0.02)
        elif act == 'party':
            # Вечеринка — тот же драйв, но танцевальный: чуть мягче тренировки.
            jitter = min(float(jitter), 0.06)
            mp = [max(float(a), b) for a, b in zip(mp, [0.08, 0.15, 0.25])]
            novelty_extra = min(float(novelty_extra), 0.03)
        elif act in ('commute', 'wakeup', 'walk'):
            # В дороге/подъём/прогулка — бодро и ровно, без жёсткого удержания.
            jitter = min(float(jitter), 0.08)
            novelty_extra = min(float(novelty_extra), 0.04)
        elif act in ('rest', 'sleep'):
            jitter = min(float(jitter), 0.08)
            novelty_extra = min(float(novelty_extra), 0.04)
        # work/study — фон для работы: характер не трогаем сознательно,
        # иначе фон превращается в запрет (см. ACTIVITY_HARD).
        return jitter, mp, novelty_extra
    except Exception:
        return jitter, mood_penalty, novelty_extra


def score_candidates(db, user_id: str, candidate_ids: list[str],
                     seed_ids: list[str], settings: dict | None = None,
                     recent_events: list[dict] | None = None,
                     collab_scores: dict[str, float] | None = None,
                     current_hour: int | None = None,
                     jitter_seed: str | None = None,
                     drift: dict | None = None,
                     ref_key: tuple | None = None,
                     cluster_map: dict[str, int] | None = None,
                     seed_clusters: set[int] | None = None,
                     ref_sentiment: str | None = None,
                     target_sentiment: str | None = None,
                     session_ids: list[str] | None = None,
                     neg_ids: list[str] | None = None,
                     last_played: dict[str, Any] | None = None,
                     fatigue: dict[str, int] | None = None,
                     time_map: dict[str, float] | None = None,
                     arm_artist: dict[str, float] | None = None,
                     arm_genre: dict[str, float] | None = None,
                     assoc: dict[str, float] | None = None,
                     clap_map: dict | None = None,
                     clap_weight: float | None = None,
                     mood_arm: dict[str, float] | None = None,
                     cur_energy: float | None = None,
                     mood_target: dict[str, float] | None = None,
                     mood_radius: float | None = None,
                     genre_vecs: dict[str, dict[str, float]] | None = None
                     ) -> list[dict]:
    """Порт TrackScorer.scoreAndRankTracks на наших таблицах.

    jitter_seed: детерминированный per-user джиттер вместо глобального random
    (иначе у юзеров без данных порядок одинаковый — все вкусовые члены нули).
    """
    from app.db.models import Track, TrackFeatures
    from app.services import taste as _taste
    from app.services.ml import _cosine, _feature_vector

    settings = settings or {}
    tun = _tun_all(db, user_id)
    _rng = random.Random(jitter_seed) if jitter_seed else random
    mood = _norm_mood(settings.get('mood'))
    try:
        from app.core.time import local_hour as _local_hour

        hour = current_hour if current_hour is not None else _local_hour()
    except Exception:
        hour = current_hour if current_hour is not None else datetime.now().hour
    try:
        from app.core.time import server_now as _server_now2

        _dow = _server_now2().isoweekday() % 7
    except Exception:
        _dow = datetime.now().isoweekday() % 7
    # Обученный скип-предиктор: один раз на вызов (TTL-кэш внутри get_model).
    # Нет данных — None, и кандидаты идут на эвристике skip_risk().
    _skip_pipe = None
    try:
        from app.services import skip_model as _sm0

        _skip_pipe = _sm0.get_model(db)
    except Exception:
        _skip_pipe = None
    # Пресеты: код пилюли (любая форма -> канонический) -> диапазоны фичей.
    # Раньше ключи пресетов были русскими пилюлями, а плеер шлёт 'calm':
    # пресет не находился, и весь ctx-блок настроения молча выключался.
    _mood_preset = MOOD_PRESETS.get(mood or '', {})
    _act_code = _norm_activity(settings.get('activity'))
    _act_preset = ACTIVITY_PRESETS.get(_act_code or '', {})
    # Семейство мудов пилюли. Если пресета нет — семейства нет (НЕ {mood}):
    # вырожденное семейство превращало фильтр в «оставь треки с этим словом»
    # и откатывалось целиком. Пустое семейство = фильтр по настроению выключен.
    _mood_family = set(_mood_preset.get('moods') or set())
    # Числовая пилюля: центр в муд-пространстве + радиус. Если пилюли нет —
    # числовой вклад выключен (всё как было).
    _mood_target = (mood_target if mood_target is not None
                    else (MOOD_TARGETS.get(mood or '') or None))
    try:
        _mood_radius = float(mood_radius) if mood_radius else MOOD_RADIUS
    except (TypeError, ValueError):
        _mood_radius = MOOD_RADIUS
    _mood_fit_w = _tun_float(tun, 'character', 'mood_fit_weight', 0.18)

    prof = _taste.user_profile(db, user_id, top_n=0)
    likes_total = int((prof.get('counts') or {}).get('likes', 0) or 0)
    w = _weights(likes_total, tun)
    pref_g = {str(k).lower(): float(v) for k, v in
              (prof.get('preferredGenres') or {}).items()}
    pref_a = {str(k): float(v) for k, v in
              (prof.get('preferredArtists') or {}).items()}

    # Агрегаты поведения (playCount/replay для novelty, score для behavior)
    agg = _taste._user_track_aggregates(db, user_id)

    _all_feat_ids = list(dict.fromkeys(
        list(candidate_ids or []) + list(seed_ids or []) +
        list(session_ids or []) + list(neg_ids or [])[:500]))
    feats: dict[str, Any] = {}
    if _all_feat_ids:
        for i in range(0, len(_all_feat_ids), 500):
            try:
                for f in db.query(TrackFeatures).filter(
                        TrackFeatures.track_id.in_(
                            _all_feat_ids[i:i + 500])).all():
                    feats[str(f.track_id)] = f
            except Exception:
                pass
    seed_vecs = [v for v in
                 (_feature_vector(None, feats[s]) for s in seed_ids if s in feats)
                 if v is not None]
    # Средний fingerprint волны = центроид сидов (у мобилы — rolling fingerprint)
    import numpy as _np
    centroid = None
    if seed_vecs:
        try:
            centroid = _np.mean(_np.vstack(seed_vecs), axis=0)
        except Exception:
            centroid = None
    # Сессионный fingerprint (порт mobile sonic_fingerprint): куда увёл
    # пользователь прямо сейчас — центроид недавно игранного.
    sess_centroid = None
    try:
        _sv = [v for v in
               (_feature_vector(None, feats[s]) for s in (session_ids or [])
                if s in feats) if v is not None]
        if _sv:
            sess_centroid = _np.mean(_np.vstack(_sv), axis=0)
    except Exception:
        sess_centroid = None
    # Негативный вектор вкуса: центроид дизлайков — похожее штрафуем.
    neg_centroid = None
    try:
        _nv = [v for v in
               (_feature_vector(None, feats[s]) for s in (neg_ids or [])[:500]
                if s in feats) if v is not None]
        if _nv:
            neg_centroid = _np.mean(_np.vstack(_nv), axis=0)
    except Exception:
        neg_centroid = None
    # CLAP-центроиды (семантика аудио, не только librosa-тембр).
    # Нет эмбеддингов/анализа → None → вклад 0, волна как раньше.
    _cw_max = _tun_float(tun, 'character', 'clap_max', 0.3)
    if clap_weight is not None:
        try:
            _cw = float(clap_weight)
        except (TypeError, ValueError):
            _cw = _clap_weight(tun)
    else:
        _cw = _clap_weight(tun)
    _cw = min(max(_cw, 0.0), max(_cw_max, 0.0))
    clap_map = clap_map or {}
    clap_centroid = None
    clap_sess = None
    if _cw > 0 and clap_map:
        try:
            _cv = [clap_map[s] for s in (seed_ids or []) if s in clap_map]
            if _cv:
                clap_centroid = _np.mean(_np.vstack(_cv), axis=0)
            _sv2 = [clap_map[s] for s in (session_ids or []) if s in clap_map]
            if _sv2:
                clap_sess = _np.mean(_np.vstack(_sv2), axis=0)
        except Exception:
            clap_centroid = None
            clap_sess = None
    else:
        _cw = 0.0

    # ---- ПОТОК (этап 3, часть 2): тембр и k-NN ---------------------------
    # Тембр (chroma + форма MFCC) и косинус к КОНКРЕТНЫМ опорным трекам
    # (k-NN) — сигналы, которых в скоринге не было. Считаются один раз на
    # вызов, не на трек. Нет анализа/эмбеддингов -> вклад 0, волна 1в1.
    _tw = _tun_float(tun, 'character', 'timbre_weight', 0.05)
    _knn_w = _tun_float(tun, 'character', 'knn_weight', 0.08)
    try:
        from app.services import flow as _flow
    except Exception:
        _flow = None
    timbre_map: dict[str, Any] = {}
    timbre_seed_cent = None
    timbre_sess_cent = None
    knn_map: dict[str, float] = {}
    knn_source: str | None = None
    # Опоры k-NN: что играет сейчас (session) + сиды вкуса.
    _flow_ref_ids: list[str] = list(dict.fromkeys(
        list(session_ids or []) + list(seed_ids or [])))
    if _flow is not None and (_tw > 0 or _knn_w > 0):
        if _tw > 0:
            try:
                for _tid, _f in feats.items():
                    if _f is None:
                        continue
                    _tv = _flow.timbre_vector(_f)
                    if _tv:
                        timbre_map[_tid] = _tv
                timbre_seed_cent = _flow.timbre_centroid(
                    [timbre_map[s] for s in (seed_ids or []) if s in timbre_map])
                timbre_sess_cent = _flow.timbre_centroid(
                    [timbre_map[s] for s in (session_ids or []) if s in timbre_map])
            except Exception:
                timbre_map, timbre_seed_cent, timbre_sess_cent = {}, None, None
        if _knn_w > 0 and _flow_ref_ids:
            try:
                # 1) CLAP-эмбеддинги (семантика звука) — если есть.
                if clap_map:
                    knn_map = _flow.knn_scores(
                        {tid: clap_map[tid] for tid in candidate_ids
                         if tid in clap_map},
                        clap_map, _flow_ref_ids)
                    if knn_map:
                        knn_source = "clap"
                # 2) Фолбэк: 9-D вектор librosa (темп/энергия/центроид/...).
                if not knn_map:
                    _fv: dict[str, Any] = {}
                    for _tid in list(candidate_ids) + _flow_ref_ids:
                        _f = feats.get(_tid)
                        if _f is None:
                            continue
                        _v = _feature_vector(None, _f)
                        if _v is not None:
                            _fv[_tid] = _v
                    knn_map = _flow.knn_scores(_fv, _fv, _flow_ref_ids)
                    if knn_map:
                        knn_source = "audio"
            except Exception:
                knn_map, knn_source = {}, None
    if timbre_map or knn_map:
        try:
            from app.core.logging import get_logger as _gl

            _gl("wave").info(
                "wave flow: тембр {} треков, k-NN {} ({}), опор {}",
                len(timbre_map), len(knn_map), knn_source or "-",
                len(_flow_ref_ids))
        except Exception:
            pass

    # behaviorBonus по свежим событиям (окно и таблица — из личного тюнинга,
    # дефолт: окно 10 как в мобиле).
    _b_win = _tun_int(tun, 'skips', 'behavior_window', 10)
    _b_like = _tun_float(tun, 'skips', 'bonus_like', 0.2)
    _b_replay = _tun_float(tun, 'skips', 'bonus_replay', 0.25)
    _b_complete = _tun_float(tun, 'skips', 'bonus_complete', 0.2)
    _b_play = _tun_float(tun, 'skips', 'bonus_play_long', 0.1)
    _play_long = _tun_int(tun, 'skips', 'play_long_sec', 180)
    _p_abandon = _tun_float(tun, 'skips', 'penalty_abandon', -0.15)
    _p_early = _tun_float(tun, 'skips', 'penalty_skip_early', -0.3)
    _early_sec = _tun_int(tun, 'skips', 'skip_early_sec', 30)
    _p_late = _tun_float(tun, 'skips', 'penalty_skip_late', -0.1)
    _late_sec = _tun_int(tun, 'skips', 'skip_late_sec', 120)
    bonus: dict[str, float] = Counter()
    for e in (recent_events or [])[:max(1, _b_win)]:
        if not isinstance(e, dict):
            continue
        tid = str(e.get('track_id') or '')
        a = str(e.get('action') or '')
        pos = e.get('position_sec')
        try:
            pos = int(pos) if pos is not None else 999
        except (TypeError, ValueError):
            pos = 999
        if a == 'like':
            bonus[tid] += _b_like
        elif a in ('seek_back', 'replay'):
            bonus[tid] += _b_replay
        elif a == 'complete':
            bonus[tid] += _b_complete
        elif a == 'play' and pos >= _play_long:
            bonus[tid] += _b_play
        elif a == 'abandon':
            bonus[tid] += _p_abandon
        elif a == 'skip' and pos < _early_sec:
            bonus[tid] += _p_early
        elif a == 'skip' and pos >= _late_sec:
            bonus[tid] += _p_late

    used_artists: dict[str, int] = {}
    used_genres: dict[str, int] = {}
    used_moods: dict[str, int] = {}
    # Личный тюнинг, один раз на вызов (не на трек): штрафы повторов,
    # усталость, recency, веса total. Битые значения -> дефолты выше.
    _ap = _tun_list(tun, 'repeats', 'artist_penalty', [0.05, 0.10, 0.15], 3)
    _gp = _tun_list(tun, 'repeats', 'genre_penalty', [0.03, 0.07, 0.12], 3)
    _mp = _tun_list(tun, 'repeats', 'mood_penalty', [0.03, 0.06, 0.10], 3)
    _ft = [int(x) for x in
           _tun_list(tun, 'repeats', 'fatigue_thresholds', [3, 6, 10], 3)]
    _fpw = _tun_list(tun, 'repeats', 'fatigue_penalty',
                     [0.05, 0.10, 0.15], 3)
    _rw = _tun_list(tun, 'repeats', 'recency_windows_h', [3, 24, 168], 3)
    _rf = _tun_list(tun, 'repeats', 'recency_factors', [0.3, 0.6, 0.85], 3)
    _nov_extra = _tun_float(tun, 'repeats', 'novelty_extra', 0.08)
    _jitter = _tun_float(tun, 'character', 'jitter', 0.12)
    # Контекстное ужесточение под занятие/настроение (см. _context_overrides):
    # пилюля на плеере даёт и фильтр, и характер — кнопок в тюнинге жать не надо.
    _jitter, _mp, _nov_extra = _context_overrides(
        _act_code, mood, _jitter, _mp, _nov_extra)
    _skip_w = _tun_float(tun, 'character', 'skip_weight', 0.25)
    _key_w = _tun_float(tun, 'character', 'key_weight', 0.06)
    _cluster_w = _tun_float(tun, 'character', 'cluster_weight', 0.06)
    _lyr_w = _tun_float(tun, 'character', 'lyrics_weight', 0.05)
    _assoc_w = _tun_float(tun, 'character', 'assoc_weight', 0.08)
    _srv_star = _tun_float(tun, 'character', 'srv_starred', 0.05)
    _srv_rate = _tun_float(tun, 'character', 'srv_rating', 0.10)
    _neg_w = _tun_float(tun, 'character', 'neg_weight', 0.15)
    _arm_min = _tun_float(tun, 'character', 'arm_min', -0.16)
    _arm_max = _tun_float(tun, 'character', 'arm_max', 0.16)
    if _arm_min > _arm_max:
        _arm_min, _arm_max = -0.16, 0.16
    _marm_min = _tun_float(tun, 'character', 'mood_arm_min', -0.08)
    _marm_max = _tun_float(tun, 'character', 'mood_arm_max', 0.12)
    if _marm_min > _marm_max:
        _marm_min, _marm_max = -0.08, 0.12
    out: list[dict] = []
    tracks = {str(t.id): t for t in
              db.query(Track).filter(Track.id.in_(candidate_ids)).all()} \
        if candidate_ids else {}
    # Векторы жанров (для треков без анализа) — считаем по пулу, без
    # дополнительного запроса: это среднее по уже загруженным фичам.
    if genre_vecs is None:
        try:
            _gm_ = {tid: (getattr(tracks.get(tid), 'genre', '') or '')
                    for tid in candidate_ids if tid in tracks}
            genre_vecs = genre_vector_map(feats, _gm_)
        except Exception:
            genre_vecs = {}
    for tid in candidate_ids:
        t = tracks.get(tid)
        if not t:
            continue
        f = feats.get(tid)
        # Муд-пространство: вектор трека. Свой анализ — уверенно; нет
        # анализа — вектор жанра (примерно, с пониженной уверенностью).
        # Трек без анализа НЕ исключается: пока идёт анализ, он может
        # попасть в волну, но и не вытесняет проверенные треки.
        _own_vec = mood_vector(f) if f is not None else {}
        _vec = _own_vec
        _vec_conf = 1.0
        if not _vec:
            _vec = (genre_vecs or {}).get(
                str(t.genre or '').strip().lower()) or {}
            _vec_conf = 0.85
        _mfit, _mdist = (0.0, 1.0)
        if _mood_target and _vec:
            _mfit, _mdist = mood_fit(_vec, _mood_target, _mood_radius)
            _mfit *= _vec_conf
        # audio: косинус к центроиду сидов + 0.4 сессионного fingerprint
        # (порт mobile: audio*0.6 + fingerprint*0.4).
        audio = 0.0
        if centroid is not None and f is not None:
            v = _feature_vector(None, f)
            if v is not None:
                try:
                    audio = float(_cosine(v, centroid))
                except Exception:
                    audio = 0.0
                if sess_centroid is not None:
                    try:
                        audio = audio * 0.6 + float(_cosine(v, sess_centroid)) * 0.4
                    except Exception:
                        pass
        # Негативный вектор: похожее на задизлайканное — штраф.
        neg_pen = 0.0
        if neg_centroid is not None and f is not None:
            try:
                v = _feature_vector(None, f)
                if v is not None:
                    neg_pen = max(0.0, float(_cosine(v, neg_centroid))) * _neg_w
            except Exception:
                neg_pen = 0.0
        genre_s = 0.0
        if t.genre:
            genre_s = min(max(pref_g.get(str(t.genre).lower(), 0.0) / 10.0, 0.0), 1.0)
        artist_s = 0.0
        if t.artist_name:
            artist_s = min(max(pref_a.get(str(t.artist_name), 0.0) / 10.0, 0.0), 1.0)
        a = agg.get(tid, {})
        behavior = 0.0
        if a:
            try:
                behavior = min(max((_taste.track_score(a) + 50.0) / 100.0, 0.0), 1.0)
            except Exception:
                behavior = 0.0
        plays = int(a.get('plays', 0) or 0) if a else 0
        replays = int(a.get('replays', 0) or 0) if a else 0
        novelty = 1.0 / (1.0 + plays + replays * 2) if (plays or replays) else 1.0
        novelty = min(max(novelty, 0.0), 1.0)
        # Временное затухание (порт mobile recency): слышал час назад —
        # не stadiть в очередь, даже если счётчики маленькие.
        if last_played and tid in last_played:
            try:
                _age_h = (datetime.utcnow() - last_played[tid]).total_seconds() / 3600.0
                if _age_h < _rw[0]:
                    novelty *= _rf[0]
                elif _age_h < _rw[1]:
                    novelty *= _rf[1]
                elif _age_h < _rw[2]:
                    novelty *= _rf[2]
            except Exception:
                pass
        collab = min(max(float((collab_scores or {}).get(tid, 0.0)), 0.0), 1.0)
        # «Часто в этот час» (порт mobile timeBonus).
        time_b = 0.0
        if time_map:
            try:
                time_b = min(max(float(time_map.get(tid, 0.0)), 0.0), 0.2)
            except (TypeError, ValueError):
                time_b = 0.0
        # Exploit бандита: руки артиста/жанра в текущем контексте.
        arm_b = 0.0
        try:
            if arm_artist and t.artist_name:
                arm_b += float(arm_artist.get(t.artist_name, 0.0))
            if arm_genre and t.genre:
                arm_b += float(arm_genre.get(str(t.genre).lower(), 0.0))
            arm_b = min(max(arm_b, _arm_min), _arm_max)
        except (TypeError, ValueError):
            arm_b = 0.0
        # Ассоциация по сессиям (порт mobile item_similarity).
        assoc_b = 0.0
        if assoc:
            try:
                assoc_b = min(max(float(assoc.get(tid, 0.0)), 0.0), 1.0) * _assoc_w
            except (TypeError, ValueError):
                assoc_b = 0.0
        # Рейтинг/звёздочка из Navidrome (порт mobile serverScore).
        srv_bonus = 0.0
        try:
            if getattr(t, "starred", False):
                srv_bonus += _srv_star
            _rt = getattr(t, "rating", None)
            if _rt:
                srv_bonus += min(max(float(_rt) / 5.0, 0.0), 1.0) * _srv_rate
        except (TypeError, ValueError):
            pass
        # Тональность: совместимость с текущим треком (плавный переход).
        key_c = 0.5
        if ref_key:
            try:
                from app.services.orchestrator import key_compatibility as _kc

                key_c = float(_kc(ref_key[0], ref_key[1],
                                  getattr(f, "key_name", None) if f is not None else None,
                                  getattr(f, "scale", None) if f is not None else None))
            except Exception:
                key_c = 0.5
        # KMeans-кластер: та же «полка» библиотеки, что у сидов вкуса.
        cluster_c = 0.0
        if seed_clusters and cluster_map:
            try:
                cluster_c = 1.0 if cluster_map.get(tid) in seed_clusters else 0.0
            except Exception:
                cluster_c = 0.0
        # Настроение лирики (lyrics-AI sentiment): к целевому муд-пресету,
        # иначе — к текущему треку (держать вайб).
        lyr_c = 0.5
        try:
            mv = (f.mood_vector if f is not None and isinstance(
                getattr(f, "mood_vector", None), dict) else {}) or {}
            want = target_sentiment or ref_sentiment
            got = str(mv.get("ai_sentiment") or "").lower() or None
            if want and got:
                lyr_c = 1.0 if got == want else (0.5 if "neutral" in (got, want) else 0.0)
        except Exception:
            lyr_c = 0.5

        ctx = 0.0
        if f is not None:
            try:
                en = float(f.energy or 0.5)
            except (TypeError, ValueError):
                en = 0.5
            try:
                bpm = float(f.tempo_bpm or 0)
            except (TypeError, ValueError):
                bpm = 0
            try:
                va = float(f.valence if f.valence is not None else 0.5)
            except (TypeError, ValueError):
                va = 0.5
            try:
                da = float(f.danceability if f.danceability is not None else 0.5)
            except (TypeError, ValueError):
                da = 0.5
            # legacy активности (совместимость): те же коды, что в пресетах
            if _act_code == 'work' and 0.3 <= en <= 0.6:
                ctx += 0.1
            if _act_code == 'workout' and bpm > 110:
                ctx += 0.15
            if _act_code == 'sleep' and en < 0.3:
                ctx += 0.1
            if mood and (_mood_codes_of(f) & _mood_family):
                ctx += 0.1
            # пресеты настроения: родственные муд-лейблы + диапазоны
            if _mood_preset:
                _fm = _mood_codes_of(f)
                if _fm & _mood_family:
                    ctx += 0.12
                _ok = True
                if 'energy_min' in _mood_preset and en < _mood_preset['energy_min']:
                    _ok = False
                if 'energy_max' in _mood_preset and en > _mood_preset['energy_max']:
                    _ok = False
                if 'valence_min' in _mood_preset and va < _mood_preset['valence_min']:
                    _ok = False
                if 'valence_max' in _mood_preset and va > _mood_preset['valence_max']:
                    _ok = False
                if 'dance_min' in _mood_preset and da < _mood_preset['dance_min']:
                    _ok = False
                if 'tempo_min' in _mood_preset and bpm and bpm < _mood_preset['tempo_min']:
                    _ok = False
                if 'tempo_max' in _mood_preset and bpm and bpm > _mood_preset['tempo_max']:
                    _ok = False
                # arousal_max/min был в пресетах, но никогда не проверялся:
                # «спокойное» с arousal 0.8 проходило как спокойное.
                try:
                    ar = float(f.arousal) if f.arousal is not None else None
                except (TypeError, ValueError):
                    ar = None
                if ar is not None:
                    if 'arousal_min' in _mood_preset and ar < _mood_preset['arousal_min']:
                        _ok = False
                    if 'arousal_max' in _mood_preset and ar > _mood_preset['arousal_max']:
                        _ok = False
                if _ok:
                    ctx += 0.08
            # пресеты занятия: мягкие бонусы
            if _act_preset:
                _aok = True
                if 'energy_min' in _act_preset and en < _act_preset['energy_min']:
                    _aok = False
                if 'energy_max' in _act_preset and en > _act_preset['energy_max']:
                    _aok = False
                if 'tempo_min' in _act_preset and bpm and bpm < _act_preset['tempo_min']:
                    _aok = False
                if 'tempo_max' in _act_preset and bpm and bpm > _act_preset['tempo_max']:
                    _aok = False
                if 'valence_min' in _act_preset and va < _act_preset['valence_min']:
                    _aok = False
                if _aok:
                    ctx += 0.10
            # время суток: утром — энергия, вечером — спокойствие
            if 6 <= hour <= 10 and en >= 0.6:
                ctx += 0.03
            elif hour >= 22 or hour <= 5:
                if en <= 0.5:
                    ctx += 0.03
            # сессионный дрейф (порт MoodDriftDetector): скипы подряд остужают
            # волну — энергичное/быстрое штрафуем, спокойное чуть поднимаем.
            _dr = drift or {}
            _eshift = float(_dr.get("energy_shift") or 0.0)
            if _eshift:
                ctx += _eshift * (en - 0.4)
            _tshift = float(_dr.get("tempo_shift") or 0)
            if _tshift and bpm > 110:
                ctx += (_tshift / -30.0) * -0.15 * min(max((bpm - 110) / 50.0, 0.0), 1.0)
        # Числовая близость к пилюле: трек внутри радиуса получает бонус,
        # трек вне — ноль (не минус: радиус — это «свой диапазон», а не
        # запрет). Трек без анализа (вектор выведен из жанра) получает
        # бонус с пониженной уверенностью — он может попасть в волну,
        # пока идёт анализ, но не вытесняет проверенные треки.
        if _mfit > 0:
            ctx += _mood_fit_w * _mfit
        # diversity
        pen = 0.0
        if t.artist_name and t.artist_name in used_artists:
            c = used_artists[t.artist_name]
            pen += _ap[0] if c == 1 else (_ap[1] if c == 2 else _ap[2])
        if t.genre and t.genre in used_genres:
            c = used_genres[t.genre]
            pen += _gp[0] if c == 1 else (_gp[1] if c == 2 else _gp[2])
        # Разнообразие настроений: не класть одно и то же настроение пачкой.
        # Смотрим ВСЕ лейблы трека (агрессивный часто 2-й после энергичного),
        # берём максимальный штраф — детерминированно (sorted), без тройного штрафа.
        _cms = sorted(_mood_codes_of(f)) if f is not None else []
        if _cms:
            _best = 0.0
            for _mc in _cms:
                if _mc in used_moods:
                    c = used_moods[_mc]
                    _cand_pen = _mp[0] if c == 1 else (_mp[1] if c == 2 else _mp[2])
                    if _cand_pen > _best:
                        _best = _cand_pen
            pen += _best
        _cm = _cms[0] if _cms else (_mood_of(f) if f is not None else None)
        # Усталость артиста за неделю (по истории): загонянное остужаем.
        if fatigue and t.artist_name and t.artist_name in fatigue:
            _fp = int(fatigue[t.artist_name] or 0)
            if _fp > _ft[2]:
                pen += _fpw[2]
            elif _fp >= _ft[1]:
                pen += _fpw[1]
            elif _fp >= _ft[0]:
                pen += _fpw[0]

        # CLAP-семантика: косинус к центроиду сидов + сессия (как audio).
        # Нет эмбеддинга у трека/сидов → 0, волна 1в1 как раньше.
        clap_s = 0.0
        if _cw > 0 and tid in (clap_map or {}):
            try:
                _cv = clap_map[tid]
                if clap_centroid is not None:
                    clap_s = float(_cosine(_cv, clap_centroid))
                    if clap_sess is not None:
                        clap_s = clap_s * 0.6 + float(_cosine(_cv, clap_sess)) * 0.4
                elif clap_sess is not None:
                    clap_s = float(_cosine(_cv, clap_sess))
                clap_s = min(max(clap_s, 0.0), 1.0)
            except Exception:
                clap_s = 0.0
        # Тембр: сходство МАТЕРИАЛА трека (гармоника + форма) с сидами и
        # сессией. Один жанр — разный материал: ударные против струнных.
        timbre_c = 0.0
        if timbre_map and _flow is not None:
            _tv = timbre_map.get(tid)
            if _tv is not None:
                _parts: list[tuple[float, float]] = []
                if timbre_seed_cent is not None:
                    _parts.append((0.6, _flow.timbre_to_centroid(
                        _tv, timbre_seed_cent)))
                if timbre_sess_cent is not None:
                    _parts.append((0.4, _flow.timbre_to_centroid(
                        _tv, timbre_sess_cent)))
                if _parts:
                    _wsum = sum(w for w, _ in _parts)
                    timbre_c = sum(w * v for w, v in _parts) / _wsum
                    timbre_c = min(max(timbre_c, 0.0), 1.0)
        # k-NN: похожесть на КОНКРЕТНЫЕ опорные треки, а не на их среднее.
        # Трек, похожий на один играющий трек, получает реальный балл.
        knn_c = 0.0
        if knn_map:
            try:
                knn_c = min(max(float(knn_map.get(tid, 0.0)), 0.0), 1.0)
            except (TypeError, ValueError):
                knn_c = 0.0
        # Ручка настроения бандита (mood-армы): любит такое в этот контекст.
        # Берём лучший из всех лейблов трека (см. выше про 2-й агрессивный).
        mood_b = 0.0
        try:
            if mood_arm and _cms:
                _best_b = 0.0
                for _mc in _cms:
                    _v = min(max(float(mood_arm.get(_mc, 0.0)), _marm_min), _marm_max)
                    if _v > _best_b:
                        _best_b = _v
                mood_b = _best_b
            elif mood_arm and _cm:
                mood_b = min(max(float(mood_arm.get(_cm, 0.0)), _marm_min), _marm_max)
        except (TypeError, ValueError):
            mood_b = 0.0
        # Предикт скипа <30с: обученная модель, иначе эвристика.
        skip_p = _skip_prob(_skip_pipe, t, f, pref_g, cur_energy, drift,
                            fatigue, agg.get(tid), hour, _dow)
        # Штраф за трек, который НЕ подтверждает выбранное настроение.
        # Раньше трек без анализа проходил наравне с проверенным: звука мы
        # о нём не знаем, а жанр (если известен) мог прямо противоречить
        # пилюле. Поэтому «Боевое» звучало вполсилы — в очереди оказывался
        # лоу-фай рэп, потому что для него просто не было цифр.
        mood_pen = 0.0
        if _mood_family:
            try:
                if f is None:
                    if _mood_target and _vec:
                        # Вектор выведен из жанра: оцениваем по расстоянию.
                        # Внутри радиуса — трек подходит (штрафа нет);
                        # снаружи — штраф растёт с расстоянием.
                        if _mdist > _mood_radius:
                            mood_pen = min(
                                0.15, 0.05 + 0.5 * (_mdist - _mood_radius))
                    else:
                        _gm = _genre_mood(t.genre)
                        mood_pen = 0.15 if (_gm is not None and
                                            _gm not in _mood_family) else 0.05
                elif not (_mood_codes_of(f) & _mood_family):
                    # Слова от LLM — про ТЕКСТ песни, а не про звук. Если трек
                    # по звуку внутри пилюли — слово не важно.
                    if _mood_target and _mdist <= _mood_radius:
                        mood_pen = 0.0
                    else:
                        mood_pen = 0.10
            except Exception:
                mood_pen = 0.0
        _jit_v = _rng.random() * _jitter
        total_raw = (w['audio'] * audio + w['genre'] * genre_s +
                     w['artist'] * artist_s + w['behavior'] * behavior +
                     w['collab'] * collab + w['novelty'] * novelty +
                     _nov_extra * novelty + ctx + bonus.get(tid, 0.0) -
                     pen - neg_pen - mood_pen + srv_bonus + time_b + arm_b + assoc_b +
                     _cw * clap_s + mood_b - _skip_w * skip_p +
                     _jit_v +
                     _key_w * key_c + _cluster_w * cluster_c + _lyr_w * lyr_c +
                     _tw * timbre_c + _knn_w * knn_c)
        # Кламп 0..1 — ТОЛЬКО для показа. Раньше он же был и рангом: сумма
        # потолков (~2.6) упирает в 1.0 почти всю пачку, и порядок решался
        # остатками (то есть порядком обхода пула), а не моделью. Теперь
        # сортируем по сырому score_raw, а 'score' остаётся 0..1 для клиента.
        total = min(max(total_raw, 0.0), 1.0)
        # Причина: сначала — прямое попадание в настроение (его выбрал
        # пользователь, это самое точное объяснение), потом остальные.
        _mood_hit = bool(_mood_family and f is not None and
                         (_mood_codes_of(f) & _mood_family))
        if _mood_hit and mood:
            reason = f'Под настроение: {MOOD_LABELS.get(mood, mood)}'
        elif collab >= 0.7:
            reason = 'Loved by friends'
        elif audio > 0.8:
            reason = 'Similar to what you love'
        elif artist_s > 0.5:
            reason = f'By {t.artist_name}'
        elif genre_s > 0.5:
            reason = f'{t.genre} you enjoy'
        elif novelty > 0.8:
            reason = 'New discovery'
        else:
            reason = None
        out.append({'track_id': tid, 'external_id': str(t.external_id or ''),
                    'title': t.title,
                    'artist_name': t.artist_name, 'album_name': t.album_name,
                    'genre': t.genre, 'score': round(total, 4),
                    # Сырой скор модели (без клампа) — по нему идёт
                    # ранжирование и в БД пишется он, а не растянутый
                    # для показа min-max.
                    'score_raw': round(total_raw, 4),
                    'audio': round(audio, 3), 'reason': reason,
                    # Канонический код настроения трека — по нему считаются
                    # капы разнообразия при отборе пачки.
                    'mood_code': _cm,
                    'comp': {'audio': round(audio, 3),
                             'genre': round(genre_s, 3),
                             'artist': round(artist_s, 3),
                             'behavior': round(behavior, 3),
                             'collab': round(collab, 3),
                             'novelty': round(novelty, 3),
                             'key': round(key_c, 3),
                             'cluster': round(cluster_c, 3),
                             'lyrics': round(lyr_c, 3),
                             'neg': round(neg_pen, 3),
                             'srv': round(srv_bonus, 3),
                             'time': round(time_b, 3),
                             'arm': round(arm_b, 3),
                             'assoc': round(assoc_b, 3),
                             'clap': round(clap_s, 3),
                             'timbre': round(timbre_c, 3),
                             'knn': round(knn_c, 3),
                             'skip': round(skip_p, 3),
                             'mood_arm': round(mood_b, 3),
                             'mood_pen': round(mood_pen, 3),
                             'mood_fit': round(_mfit, 3),
                             'mood_dist': round(_mdist, 3)}})
        if t.artist_name:
            used_artists[t.artist_name] = used_artists.get(t.artist_name, 0) + 1
        if t.genre:
            used_genres[t.genre] = used_genres.get(t.genre, 0) + 1
        if _cm:
            used_moods[_cm] = used_moods.get(_cm, 0) + 1
    # Ранжирование — по сырому скору модели: кламп 0..1 упирал почти всю
    # пачку в 1.0, и треки с разным качеством шли в случайном порядке.
    out.sort(key=lambda x: (x.get('score_raw')
                             if x.get('score_raw') is not None else x['score']),
             reverse=True)
    return out


def spread_scores(items: list[dict], key: str = "score") -> list[dict]:
    """Растянуть скоры окна в 0..1 (min-max), чтобы топ не выглядел как
    сплошные 1.00: сырые total часто упираются в кламп 1.0.
    Монотонно — порядок не меняется, только видимая дифференциация.

    Растягиваем по СЫРОМУ скору (score_raw), если он есть: по клампнутому
    min-max бесполезен (половина пачки — ровно 1.0), и топ опять выглядит
    как «1.00 × N». Сам score_raw не трогаем — он идёт в БД как оценка
    модели (см. rec_feedback.mark_served)."""
    try:
        vals = [float(it.get('score_raw')
                      if it.get('score_raw') is not None else (it.get(key) or 0))
                for it in items]
    except Exception:
        return items
    if len(vals) < 2:
        return items
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return items
    for it, v in zip(items, vals):
        try:
            it[key] = round((v - lo) / (hi - lo), 3)
        except Exception:
            pass
    return items


def rerank_pool(db, user_id: str, candidate_ids: list[str], *,
                seeds: list[str] | None = None, kind: str = "",
                day: str | None = None, settings: dict | None = None,
                n: int = 30, source: str | None = None) -> list[str]:
    """Единый скоринг плейлистов на ядре волны.

    Плейлистные генерации (daily/smart/weekly/discovery) раньше считали
    каждая свой скор; теперь все отдают свой предфильтрованный пул сюда и
    получают порядок от общего ядра: CLAP, skip-risk (модель/эвристика),
    UCB-руки, auto-mood, коллаборативка, новизна. Предфильтры генераций
    (предикаты night/sport, forgotten-окно, playable) живут у вызывающих —
    характер плейлиста не меняется, меняется только ранжирование внутри.

    Возвращает топ-n track_id. Выдачу фиксирует в rec_feedback (source) —
    это же и обучающая выборка для скип-предиктора.
    """
    from datetime import date as _date

    cand = [str(c) for c in (candidate_ids or []) if c]
    if not cand:
        return []
    n = max(1, min(100, int(n or 30)))
    # Тюнинг пула/сидов/усталости — те же ручки, что и в волне (кнопки были
    # пустыми: значения игнорировались, всё было зашито константами).
    _lim = wave_limits(db, user_id)
    _pool_limit = _lim["pool_limit"]
    _score_win = _lim["score_window"]
    _collab_extra = _lim["collab_extra"]
    _seed_limit = _lim["seed_limit"]
    _fatigue_days = _lim["fatigue_days"]
    try:
        from app.core.time import local_hour as _lh, local_today as _lt

        _hour = _lh()
        _day = day or _lt().isoformat()
    except Exception:
        from datetime import datetime as _dt

        _hour = _dt.now().hour
        _day = day or _date.today().isoformat()
    # сиды вкуса
    _seeds = [str(s) for s in (seeds or []) if s]
    if not _seeds:
        try:
            _seeds = select_seeds(db, user_id, limit=_seed_limit)
        except Exception:
            _seeds = []

    # свежие события -> дрейф + behaviorBonus (последние 60)
    recent_events: list[dict] = []
    try:
        from app.db.models import PlayEvent as _PE

        for r in db.query(_PE).filter(_PE.user_id == str(user_id)).order_by(
                _PE.created_at.desc()).limit(60).all():
            recent_events.append({"track_id": str(r.track_id),
                                  "action": r.action,
                                  "position_sec": r.position_sec})
        recent_events.reverse()
    except Exception:
        recent_events = []
    try:
        drift = session_drift(db, recent_events, user_id)
    except Exception:
        drift = {}
    # коллаборативка
    collab_scores: dict[str, float] = {}
    try:
        from app.services import collab as _cb

        rec = _cb.recommend_for_user(db, user_id, n=200)
        for it in rec.get("items", []):
            collab_scores[str(it["track_id"])] = float(it.get("score", 0) or 0)
    except Exception:
        pass
    # кластеры сидов
    cluster_map: dict[str, int] = {}
    seed_clusters: set[int] = set()
    try:
        from app.db.models import TrackCluster as _TC

        _need = list({str(c) for c in (cand[:_score_win] + _seeds)})
        for i in range(0, len(_need), 500):
            try:
                for row in db.query(_TC).filter(
                        _TC.algorithm == "kmeans",
                        _TC.track_id.in_(_need[i:i + 500])).all():
                    cluster_map[str(row.track_id)] = int(row.cluster_id)
            except Exception:
                pass
        seed_clusters = {cluster_map[s] for s in _seeds if s in cluster_map}
    except Exception:
        pass
    # негатив, recency, усталость
    try:
        from app.db.models import TrackDislike as _TD

        neg_ids = [str(r.track_id) for r in
                   db.query(_TD).filter_by(user_id=str(user_id))
                   .order_by(_TD.created_at.desc()).limit(500).all()]
    except Exception:
        neg_ids = []
    last_played: dict[str, Any] = {}
    try:
        from app.db.models import PlayHistory as _PH

        for i in range(0, len(cand[:_score_win]), 500):
            try:
                for tid, when in db.query(_PH.track_id, _PH.played_at).filter(
                        _PH.user_id == str(user_id),
                        _PH.track_id.in_(cand[:_score_win][i:i + 500])).all():
                    if when and (str(tid) not in last_played or
                                 when > last_played[str(tid)]):
                        last_played[str(tid)] = when
            except Exception:
                pass
    except Exception:
        pass
    fatigue: dict[str, int] = {}
    last_ids: list[str] = []
    try:
        from datetime import datetime as _dt2
        from datetime import timedelta as _td2

        from app.db.models import PlayHistory as _PH2
        from app.db.models import Track as _T2

        _since = _dt2.utcnow() - _td2(days=_fatigue_days)
        _recent = [str(r[0]) for r in
                   db.query(_PH2.track_id).filter(
                       _PH2.user_id == str(user_id),
                       _PH2.played_at >= _since)
                   .order_by(_PH2.played_at.desc()).limit(5000).all()]
        last_ids = list(dict.fromkeys(_recent))[:20]
        if _recent:
            _amap: dict[str, str] = {}
            _uniq = list(dict.fromkeys(_recent))
            for i in range(0, len(_uniq), 500):
                try:
                    for t in db.query(_T2).filter(
                            _T2.id.in_(_uniq[i:i + 500])).all():
                        if t.artist_name:
                            _amap[str(t.id)] = t.artist_name
                except Exception:
                    pass
            for tid in _recent:
                _an = _amap.get(tid)
                if _an:
                    fatigue[_an] = fatigue.get(_an, 0) + 1
    except Exception:
        pass
    session_ids = last_ids[:20]
    # якорь энергии — последний игранный
    cur_energy: float | None = None
    try:
        if last_ids:
            from app.db.models import TrackFeatures as _TF3

            _lf = db.query(_TF3).filter(_TF3.track_id == last_ids[0]).first()
            if _lf is not None and _lf.energy is not None:
                cur_energy = float(_lf.energy)
    except Exception:
        cur_energy = None
    # контекст: час, руки, ассоциации, CLAP, авто-муд
    try:
        from app.core.time import server_now as _sn

        _dow = _sn().isoweekday() % 7
    except Exception:
        from datetime import datetime as _dt3

        _dow = _dt3.now().isoweekday() % 7
    try:
        from app.services.taste import context_of as _ctx_of

        _ctx = _ctx_of(_hour, _dow)
    except Exception:
        _ctx = "day_wd"
    time_map = time_bonus_for(db, user_id, cand[:_score_win], _hour)
    try:
        arm_artist, arm_genre = arm_boost_for(db, user_id, _ctx)
    except Exception:
        arm_artist, arm_genre = {}, {}
    try:
        mood_arm_map = mood_boost_for(db, user_id, _ctx)
    except Exception:
        mood_arm_map = {}
    try:
        assoc = session_assoc(db, user_id, _seeds)
    except Exception:
        assoc = {}
    try:
        _clap_ids = list(dict.fromkeys(
            list(cand[:_score_win]) + list(_seeds) + list(session_ids)))[:1500]
        clap_map = _load_clap_map(db, _clap_ids)
    except Exception:
        clap_map = {}
    settings_eff = dict(settings or {})
    try:
        if not (settings_eff.get("mood") or "").strip():
            _am = infer_auto_mood(db, user_id, session_ids, _hour)
            if _am:
                settings_eff["mood"] = _am
    except Exception:
        pass
    ranked = score_candidates(
        db, user_id, cand[:_score_win], _seeds, settings_eff, recent_events,
        collab_scores, current_hour=_hour,
        jitter_seed=f"{user_id}:{kind or 'pool'}:{_day}",
        drift=drift, cluster_map=cluster_map or None,
        seed_clusters=seed_clusters or None,
        session_ids=session_ids or None, neg_ids=neg_ids or None,
        last_played=last_played or None, fatigue=fatigue or None,
        time_map=time_map or None, arm_artist=arm_artist or None,
        arm_genre=arm_genre or None, assoc=assoc or None,
        clap_map=clap_map or None, clap_weight=None,
        mood_arm=mood_arm_map or None, cur_energy=cur_energy)
    # Разнообразие пачки — как в волне: кап артиста/жанра, но качество
    # важнее (кап не тащит в плейлист заведомо худшие треки).
    top = select_diverse(ranked, n, artist_cap=2)
    try:
        from app.services import rec_feedback as _rfb

        _rfb.mark_served(db, user_id, top, source=source or kind or "wave")
    except Exception:
        pass
    return [str(r["track_id"]) for r in top if r.get("track_id")]


def _recency_w(last) -> float:
    """Затухание паттерна по давности (порт mobile time_aware_history)."""
    if not last:
        return 0.05
    try:
        from datetime import datetime as _dt

        age_d = (_dt.utcnow() - last).total_seconds() / 86400.0
    except Exception:
        return 0.05
    if age_d < 1:
        return 1.0
    if age_d < 2:
        return 0.8
    if age_d < 4:
        return 0.6
    if age_d < 8:
        return 0.3
    if age_d < 31:
        return 0.1
    return 0.05


def time_bonus_for(db, user_id: str, track_ids: list[str], hour: int) -> dict[str, float]:
    """«Часто в этот час» (порт mobile): сумма по h-1/h/h+1
    10*(1+plays/5)*recency, clamp 0..50, /50. Возвращает 0..time_max на трек."""
    out: dict[str, float] = {}
    if not track_ids:
        return out
    _tmax = _tun_float(_tun_all(db, user_id), 'character', 'time_max', 0.2)
    try:
        from app.db.models import TrackTimeStat as _TTS

        hours = {(int(hour) - 1) % 24, int(hour) % 24, (int(hour) + 1) % 24}
        rows: dict[str, float] = {}
        uniq = list(dict.fromkeys(map(str, track_ids)))
        for i in range(0, len(uniq), 500):
            try:
                for r in db.query(_TTS).filter(
                        _TTS.user_id == str(user_id),
                        _TTS.hour.in_(list(hours)),
                        _TTS.track_id.in_(uniq[i:i + 500])).all():
                    rows[str(r.track_id)] = rows.get(str(r.track_id), 0.0) + \
                        10.0 * (1.0 + float(r.plays or 0) / 5.0) * _recency_w(r.last_played)
            except Exception:
                pass
        for tid, v in rows.items():
            out[tid] = min(max(v, 0.0), 50.0) / 50.0 * max(_tmax, 0.0)
    except Exception:
        pass
    return out


def arm_boost_for(db, user_id: str, context: str) -> tuple[dict[str, float], dict[str, float]]:
    """Бандит UCB-lite: exploit среднего + направленный explore неуверенности.

    Было: средний reward -> буст -0.08..+0.08, explore только слепым
    джиттером random*0.12. Стало: exploit тот же + бонус неуверенности
    sqrt(log(total+1)/(pulls+1)) — малоигранные руки в этом контексте
    получают шанс показаться, а не тонут. Возвращает (artist, genre),
    значения -0.08..+0.12. Сигнатура и таблица те же, миграций нет.
    """
    ab: dict[str, float] = {}
    gb: dict[str, float] = {}
    try:
        import math as _math

        from app.db.models import TasteArm as _TA

        rows = db.query(_TA).filter(
            _TA.user_id == str(user_id), _TA.context == str(context)).all()
        total = sum(int(r.pulls or 0) for r in rows) or 0
        for r in rows:
            try:
                if r.kind not in ("artist", "genre"):
                    continue
                pulls = int(r.pulls or 0)
                if pulls <= 0:
                    continue
                avg = float(r.reward or 0.0) / pulls
                exploit = min(max(avg / 10.0, -1.0), 1.0) * 0.08
                explore = 0.05 * _math.sqrt(
                    _math.log(total + 1.0) / (pulls + 1.0)) if total else 0.0
                b = min(max(exploit + explore, -0.08), 0.12)
                if r.kind == "artist":
                    ab[str(r.name)] = b
                else:
                    gb[str(r.name).lower()] = b
            except Exception:
                continue
    except Exception:
        pass
    return ab, gb


def mood_boost_for(db, user_id: str, context: str) -> dict[str, float]:
    """Буст настроения-руки (kind='mood') в контексте. Нет рук → {}.

    Пишется в update_taste_signals best-effort; старые базы без mood-рук
    просто дают пусто и ничего не ломают.
    """
    out: dict[str, float] = {}
    try:
        import math as _math

        from app.db.models import TasteArm as _TA

        rows = db.query(_TA).filter(
            _TA.user_id == str(user_id), _TA.context == str(context),
            _TA.kind == "mood").all()
        total = sum(int(r.pulls or 0) for r in rows) or 0
        for r in rows:
            try:
                pulls = int(r.pulls or 0)
                if pulls <= 0:
                    continue
                avg = float(r.reward or 0.0) / pulls
                exploit = min(max(avg / 10.0, -1.0), 1.0) * 0.08
                explore = 0.04 * _math.sqrt(
                    _math.log(total + 1.0) / (pulls + 1.0)) if total else 0.0
                out[str(r.name).lower()] = min(max(exploit + explore, -0.08), 0.12)
            except Exception:
                continue
    except Exception:
        pass
    return out


def session_assoc(db, user_id: str, seed_ids: list[str],
                  limit_events: int = 1500, gap_min: int = 30) -> dict[str, float]:
    """Item-similarity по сессиям (порт mobile item_similarity, on-demand).

    Сессии — цепочки PlayHistory с разрывом >gap_min. Пара в сессии +1,
    соседние +3; sim=co/(playsA*playsB), нормировка на max; агрегация по
    сидам — MAX, сиды исключаются. Возвращает 0..1 на трек."""
    out: dict[str, float] = {}
    seeds = {str(s) for s in (seed_ids or [])}
    if not seeds:
        return out
    try:
        from datetime import datetime as _dt

        from app.db.models import PlayHistory as _PH

        rows = db.query(_PH.track_id, _PH.played_at).filter(
            _PH.user_id == str(user_id)).order_by(
            _PH.played_at.desc()).limit(max(100, limit_events)).all()
        # в хронологическом порядке, режем на сессии по разрыву
        ordered = sorted([(str(t), w) for t, w in rows if w],
                         key=lambda kv: kv[1])
        sessions: list[list[str]] = []
        cur: list[str] = []
        prev = None
        for tid, when in ordered:
            if prev is not None:
                try:
                    gap = (when - prev).total_seconds() / 60.0
                except Exception:
                    gap = 0.0
                if gap > gap_min:
                    if cur:
                        sessions.append(cur)
                    cur = []
            cur.append(tid)
            prev = when
        if cur:
            sessions.append(cur)
        from collections import Counter as _C

        co: _C = _C()
        plays: _C = _C()
        for sess in sessions[-200:]:
            for tid in sess:
                plays[tid] += 1
            for i, a in enumerate(sess):
                for j, b in enumerate(sess):
                    if a == b:
                        continue
                    if a in seeds or b in seeds:
                        co[(a, b)] += 3 if abs(i - j) == 1 else 1
        scored: dict[str, float] = {}
        for (a, b), c in co.items():
            other = b if a in seeds else (a if b in seeds else None)
            if other is None or other in seeds:
                continue
            try:
                s = float(c) / max(1, plays[a] * plays[b])
            except Exception:
                continue
            if s > scored.get(other, 0.0):
                scored[other] = s
        mx = max(scored.values()) if scored else 0.0
        if mx > 0:
            out = {tid: v / mx for tid, v in scored.items()}
    except Exception:
        pass
    return out


def adaptive_count(requested: int, drift: dict | None,
                   morphing: bool = False,
                   tun: dict | None = None) -> tuple[int, str | None]:
    """Адаптивная пачка: маленькая при смене настроения, большая в стабильном.

    Большая пачка (=10) при смене вкуса — это 30-40 минут старого вайба,
    пока хвост дослушается. Поэтому при скип-стрике и при морфинге в целевой
    муд режем пачку (пол 4 — очередь не голодает, refill и так при остатке
    <=3). В стабильном вайбе и на разогреве — полная пачка.
    Возвращает (effective, reason|None). reason None = выдали сколько просили.
    Капы — из личного тюнинга, дефолт strong 4 / moderate 5 / mild-morph 6.
    """
    tun = tun or {}
    _cap_s = _tun_int(tun, 'playlists', 'adaptive_strong', 4)
    _cap_m = _tun_int(tun, 'playlists', 'adaptive_moderate', 5)
    _cap_mild = _tun_int(tun, 'playlists', 'adaptive_mild', 6)
    _cap_morph = _tun_int(tun, 'playlists', 'adaptive_morphing', 6)
    _floor = _tun_int(tun, 'playlists', 'adaptive_floor', 4)
    try:
        n = max(1, min(100, int(requested or 20)))
    except (TypeError, ValueError):
        n = 20
    if n <= _floor:
        return n, None
    cap, bits = n, []
    sev = (drift or {}).get("severity")
    if sev == "strong":
        cap, bits = min(cap, _cap_s), bits + ["скипы ×7: разворот"]
    elif sev == "moderate":
        cap, bits = min(cap, _cap_m), bits + ["скипы ×5: быстрая пачка"]
    elif sev == "mild":
        cap, bits = min(cap, _cap_mild), bits + ["скипы ×3: пачка меньше"]
    if morphing:
        cap, bits = min(cap, _cap_morph), bits + ["переход настроения"]
    eff = max(min(cap, n), min(_floor, n))
    if eff >= n:
        return n, None
    return eff, " + ".join(bits) or None


def select_diverse(ranked: list[dict], n: int, *, artist_cap: int = 2,
                   genre_cap: int | None = None, mood_cap: int | None = None,
                   vec_map: dict[str, dict[str, float]] | None = None,
                   spread_min: float = 0.0) -> list[dict]:
    """Отбор пачки с разнообразием вместо «просто топ по скору».

    Скоринг умеет штрафовать повтор артиста/жанра, но штраф — это мягко:
    если в библиотеке 40 треков одного артиста и они все «подходят», пачка
    всё равно собиралась из них. Отсюда и жалоба «настроение одно, и
    скучно»: волна звучит как один трек на повторе.

    Правила отбора:
      * кап артиста (по умолчанию 2 трека от одного артиста в пачке);
      * кап жанра (половина пачки);
      * кап кода настроения (60 % пачки) — только когда пилюля НЕ выбрана:
        с выбранной пилюлей одно настроение в пачке это как раз замысел
        пользователя, а не монотония;
      * разброс по муд-пространству (расстояние < spread_min — «почти дубль»
        по звуку). По умолчанию выключен: признаки valence/energy грубые,
        и близкие значения ещё не означают одинаковый звук.

    ГЛАВНОЕ правило: разнообразие НЕ УХУДШАЕТ пачку. Трек под капом
    пропускается, только если есть альтернатива сопоставимого качества
    (разница по скору — в пределах floor). Иначе кап вытащил бы в пачку
    заведомо худшие треки — «разнообразие» не должно превращаться в мусор.
    """
    if not ranked:
        return []
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return []
    if not artist_cap or artist_cap <= 0:
        artist_cap = 2
    if not genre_cap or genre_cap <= 0:
        genre_cap = max(2, int(math.ceil(n * 0.5)))
    if mood_cap is not None and mood_cap <= 0:
        mood_cap = None
    vec_map = vec_map or {}
    idx_of = {id(r): i for i, r in enumerate(ranked)}

    def _score(r: dict) -> float:
        try:
            return float(r.get('score') or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # Порог качества: заменять трек в пачке можно только на сопоставимый
    # по скору. floor = 90 % от скора последнего трека обычного топа.
    base = ranked[:n]
    best = _score(base[0]) if base else 0.0
    thr = _score(base[-1]) if base else 0.0
    floor = max(thr * 0.9, best - 0.25)
    window = ranked[:max(n * 8, 60)]
    pool = [r for r in window if _score(r) >= floor] or window[:n]

    chosen: list[dict] = []
    seen: set[int] = set()
    used_a: Counter = Counter()
    used_g: Counter = Counter()
    used_m: Counter = Counter()
    chosen_vecs: list[dict[str, float]] = []

    def _violates(r: dict) -> bool:
        a = str(r.get('artist_name') or '')
        g = str(r.get('genre') or '').strip().lower()
        m = str(r.get('mood_code') or '')
        if a and used_a[a] >= artist_cap:
            return True
        if g and used_g[g] >= genre_cap:
            return True
        if mood_cap and m and used_m[m] >= mood_cap:
            return True
        return False

    def _too_close(r: dict) -> bool:
        if not (spread_min and spread_min > 0 and chosen_vecs):
            return False
        v = vec_map.get(str(r.get('track_id') or '')) or {}
        if not v:
            return False
        return any(mood_vec_dist(v, cv) < spread_min for cv in chosen_vecs)

    def _alts(start: int, need: int) -> int:
        """Сколько в остатке треков, которые капы не нарушают."""
        cnt = 0
        for j in range(start, len(pool)):
            r2 = pool[j]
            if _violates(r2) or _too_close(r2):
                continue
            cnt += 1
            if cnt >= need:
                break
        return cnt

    for i, r in enumerate(pool):
        if len(chosen) >= n:
            break
        if _violates(r) or _too_close(r):
            need = n - len(chosen) - 1
            if need > 0 and _alts(i + 1, need) >= need:
                continue
        chosen.append(r)
        seen.add(id(r))
        a = str(r.get('artist_name') or '')
        g = str(r.get('genre') or '').strip().lower()
        m = str(r.get('mood_code') or '')
        if a:
            used_a[a] += 1
        if g:
            used_g[g] += 1
        if m:
            used_m[m] += 1
        v = vec_map.get(str(r.get('track_id') or '')) or {}
        if v:
            chosen_vecs.append(v)
    if len(chosen) < n:
        for r in ranked:
            if len(chosen) >= n:
                break
            if id(r) in seen:
                continue
            chosen.append(r)
            seen.add(id(r))
    # Порядок — по исходному скору (выбор шёл по ranked, но добор мог
    # вставить трек с более высоким скором — возвращаем порядок честно).
    chosen.sort(key=lambda r: idx_of.get(id(r), 0))
    return chosen


def _ramp_desired(start_vec: dict[str, float], target_vec: dict[str, float],
                  t: float) -> dict[str, float]:
    """Желаемая точка муд-пространства на доле t пути от старта к цели."""
    out: dict[str, float] = {}
    for axis, tv in (target_vec or {}).items():
        try:
            sv = float(start_vec.get(axis, tv))
            tv = float(tv)
        except (TypeError, ValueError):
            continue
        out[axis] = sv * (1.0 - t) + tv * t
    return out


def _ramp_error(order: list[dict], start_vec: dict[str, float],
                target_vec: dict[str, float],
                vec_map: dict[str, dict[str, float]]) -> float:
    """Сумма квадратов расстояний треков до их места на лестнице."""
    n = len(order)
    if n < 2:
        return 0.0
    err = 0.0
    for i, r in enumerate(order):
        v = (vec_map or {}).get(str(r.get('track_id') or '')) or {}
        if not v:
            continue
        desired = _ramp_desired(start_vec, target_vec, i / float(n - 1))
        d = mood_vec_dist(v, desired)
        err += d * d
    return err


def order_by_mood_ramp(items: list[dict], start_vec: dict[str, float],
                       target_vec: dict[str, float],
                       vec_map: dict[str, dict[str, float]] | None = None
                       ) -> list[dict]:
    """Лестница перехода в муд-пространстве.

    Раньше градиент морфа был БИНАРНЫМ: «голова — стартовое настроение,
    хвост — целевое». Трека «наполовину грустный, наполовину
    поднимающийся» не существовало: либо грустный, либо весёлый, а
    переход — скачком. Теперь на позиции i стоит трек, ближайший к
    интерполяции от звука текущего трека к центру целевой пилюли.

    Треки без вектора (нет анализа и неизвестен жанр) в лестницу не
    встают — они уходят в хвост, порядок исходный.
    """
    n = len(items)
    if n < 2 or not target_vec:
        return items
    vec_map = vec_map or {}
    pool = list(items)
    out: list[dict] = []
    used: set[int] = set()
    for i in range(n):
        desired = _ramp_desired(start_vec, target_vec, i / float(n - 1))
        best_i = -1
        best_key = None
        for i2, r in enumerate(pool):
            if i2 in used:
                continue
            v = vec_map.get(str(r.get('track_id') or '')) or {}
            if not v:
                continue
            d = mood_vec_dist(v, desired)
            try:
                en = float(r.get('energy')
                           if r.get('energy') is not None else 0.5)
            except (TypeError, ValueError):
                en = 0.5
            de = abs(en - float(desired.get('energy', en)))
            key = (round(d, 6), round(de, 6))
            if best_key is None or key < best_key:
                best_key = key
                best_i = i2
        if best_i < 0:
            break
        used.add(best_i)
        out.append(pool[best_i])
    for i2, r in enumerate(pool):
        if i2 not in used:
            out.append(r)
    return out


def _smooth_ramp_order(items: list[dict], start_vec: dict[str, float],
                       target_vec: dict[str, float],
                       vec_map: dict[str, dict[str, float]] | None = None,
                       passes: int = 6) -> list[dict]:
    """Локальная доводка лестницы: обмен соседей, если он уменьшает
    ошибку перехода. Глобальный порядок не переставляет — только чинит
    стыки (как _smooth_keys_order, но по муд-пространству)."""
    items = list(items)
    if len(items) < 3:
        return items
    vec_map = vec_map or {}
    for _ in range(max(0, int(passes))):
        improved = False
        for i in range(len(items) - 1):
            cur = _ramp_error(items, start_vec, target_vec, vec_map)
            items[i], items[i + 1] = items[i + 1], items[i]
            if _ramp_error(items, start_vec, target_vec, vec_map) < cur - 1e-9:
                improved = True
            else:
                items[i], items[i + 1] = items[i + 1], items[i]
        if not improved:
            break
    return items


# --- Форма волны: разгон -> вершина -> спад ---------------------------------
# Монотонная раскладка по энергии (orchestrator.smooth_energy_order) даёт
# «ровную» волну: сначала тихо, потом громко — и до самого конца. Настоящая
# волна имеет ФОРМУ: разгон, вершина, спад.
#
# Форма задаётся ИНТЕНСИВНОСТЬЮ (энергия + arousal), а «цвет» трека
# (valence, dance) остаётся общим для пачки: дуга — это динамика, а не смена
# настроения. Иначе волна «переезжала» из меланхолии в эпик по дороге и
# переставала быть волной.
#
# Точка входа — интенсивность текущего трека: стык пачек не рвётся, и если
# пользователь уже слушает громко, волна не полезет ещё выше, а сойдёт.
ARC_PEAK_POS = 0.68    # где вершина (доля пачки)
ARC_RELEASE = 0.78     # во сколько спад гасит динамику


def mood_intensity(vec: dict[str, float] | None) -> float:
    """Скалярная «интенсивность» вектора: чем выше, тем громче и бодрее.

    energy 0.5 / arousal 0.3 / dance 0.2. Оси, которых нет, не считаются:
    трек без arousal не должен штрафоваться за отсутствие координаты.
    """
    num = 0.0
    den = 0.0
    for axis, w in (('energy', 0.5), ('arousal', 0.3), ('dance', 0.2)):
        if not vec or axis not in vec:
            continue
        try:
            v = float(vec[axis])
        except (TypeError, ValueError):
            continue
        num += w * min(max(v, 0.0), 1.0)
        den += w
    return (num / den) if den > 0 else 0.5


def _ease(t: float) -> float:
    """smoothstep: разгон не рвётся, вершина не «столбик»."""
    try:
        t = float(t)
    except (TypeError, ValueError):
        t = 0.0
    t = min(max(t, 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def _avg_vec(vecs: list[dict[str, float]]) -> dict[str, float]:
    """Средний вектор по списку (пропущенные оси не учитываются)."""
    acc: dict[str, float] = {}
    for v in vecs or []:
        for k, val in (v or {}).items():
            try:
                acc[k] = acc.get(k, 0.0) + float(val)
            except (TypeError, ValueError):
                continue
    n = max(1, len(vecs or []))
    return {k: val / n for k, val in acc.items()}


def wave_arc_curve(n: int, start_level: float, peak_level: float = 1.0,
                   peak_pos: float = ARC_PEAK_POS,
                   release: float = ARC_RELEASE) -> list[float]:
    """Кривая интенсивности пачки: 0 — пол, 1 — вершина.

    start_level — с какого уровня волна продолжается (интенсивность текущего
    трека, нормированная на шкалу пула). peak_level — высота вершины (1.0 —
    самая горячая трека пула). release — во сколько спад гасит динамику
    (0.78 = спад до 78 % вершины).
    """
    if n <= 0:
        return []
    p = min(max(float(peak_pos or 0.0), 0.05), 0.95)
    r = min(max(float(release or 1.0), 0.2), 1.0)
    s = min(max(float(start_level or 0.0), 0.0), 1.0)
    pk = min(max(float(peak_level or 1.0), s), 1.0)
    out: list[float] = []
    for i in range(n):
        t = i / float(n - 1) if n > 1 else 0.0
        if t <= p:
            u = (t / p) if p > 0 else 1.0
            out.append(s + (pk - s) * _ease(u))
        else:
            u = (t - p) / max(1e-6, 1.0 - p)
            out.append(pk - (pk - pk * r) * _ease(u))
    return out


def _smooth_arc_order(items: list[dict], want: list[float],
                       vec_map: dict[str, dict[str, float]],
                       passes: int = 6) -> list[dict]:
    """Локальные обмены соседей, уменьшающие ошибку дуги.

    Глобальный порядок не переставляет — чинит только стыки (как
    _smooth_keys_order, но по динамике).
    """
    items = list(items)
    if len(items) < 3:
        return items

    def _err(seq: list[dict]) -> float:
        e = 0.0
        for i, r in enumerate(seq):
            if i >= len(want):
                break
            v = (vec_map or {}).get(str(r.get('track_id') or '')) or {}
            if not v:
                continue
            d = mood_intensity(v) - want[i]
            e += d * d
        return e

    for _ in range(max(0, int(passes))):
        improved = False
        for i in range(len(items) - 1):
            cur = _err(items)
            items[i], items[i + 1] = items[i + 1], items[i]
            if _err(items) < cur - 1e-9:
                improved = True
            else:
                items[i], items[i + 1] = items[i + 1], items[i]
        if not improved:
            break
    return items


def order_by_wave_arc(items: list[dict], vec_map: dict[str, dict[str, float]],
                      *, start_vec: dict[str, float] | None = None,
                      peak_vec: dict[str, float] | None = None,
                      release: float = ARC_RELEASE,
                      peak_pos: float = ARC_PEAK_POS) -> list[dict] | None:
    """Разложить пачку по форме волны: разгон -> вершина -> спад.

    Вершина — самая горячая трека пула: пилюля задаёт ПОЛ (жёсткий фильтр
    уже отсек тихое), но не потолок — иначе треки выше пилюли некуда было бы
    девать, и хвост пачки ломался. Цвет (valence, dance) при равенстве по
    динамике тянется к центру пилюли, если она выбрана.

    Точка входа — интенсивность текущего трека, поэтому стык пачек не рвётся.

    Возвращает None, если дугу строить не из чего (нет векторов или пул
    однородный по динамике) — тогда вызывающий оставляет старую раскладку.
    """
    n = len(items)
    if n < 3 or not vec_map:
        return None
    entries: list[tuple[int, dict[str, float], float]] = []
    for idx, r in enumerate(items):
        v = vec_map.get(str(r.get('track_id') or '')) or {}
        if v:
            entries.append((idx, v, mood_intensity(v)))
    if len(entries) < 3:
        return None
    ints = sorted(e[2] for e in entries)
    lo_i, hi_i = ints[0], ints[-1]
    if hi_i - lo_i < 1e-6:
        return None  # пул однородный по динамике — дуга нечего рисовать
    # точка входа: интенсивность текущего трека
    start_level = lo_i
    if start_vec:
        start_level = min(max(mood_intensity(start_vec), lo_i), hi_i)
    level = (start_level - lo_i) / (hi_i - lo_i)
    curve = wave_arc_curve(n, level, peak_level=1.0,
                            peak_pos=peak_pos, release=release)
    want = [lo_i + c * (hi_i - lo_i) for c in curve]
    # «цвет» пачки: центр пилюли, если выбрана, иначе средний вектор пула
    ctx = dict(peak_vec) if peak_vec else _avg_vec([e[1] for e in entries])
    if not ctx:
        ctx = _avg_vec([e[1] for e in entries])
    used: set[int] = set()
    placed: list[int] = []
    for i in range(n):
        best_j = -1
        best_key = None
        for j, (_idx, v, iv) in enumerate(entries):
            if j in used:
                continue
            # главный критерий — интенсивность (форма волны), второй —
            # расстояние в муд-пространстве (среди равных по динамике)
            key = (round(abs(iv - want[i]), 6),
                   round(mood_vec_dist(v, ctx), 6))
            if best_key is None or key < best_key:
                best_key = key
                best_j = j
        if best_j < 0:
            break
        used.add(best_j)
        placed.append(entries[best_j][0])
    out = [items[i] for i in placed]
    seen = set(placed)
    out.extend(items[i] for i in range(n) if i not in seen)
    return _smooth_arc_order(out, want, vec_map)


def _smooth_energy_local(items: list[dict], max_step: float = 0.18,
                         passes: int = 8) -> list[dict]:
    """Локальное сглаживание энергии БЕЗ глобальной сортировки.

    smooth_energy_order сортирует весь набор по энергии — на морфе это
    ломало лестницу перехода (valence шёл своей дорогой). Здесь только
    обмены соседей, уменьшающие рваность: порядок лестницы сохраняется,
    а провалы по энергии сглаживаются.
    """
    items = list(items)
    if len(items) < 3:
        return items

    def _en(r: dict) -> float:
        try:
            v = r.get("energy")
            return float(v) if v is not None else 0.5
        except (TypeError, ValueError):
            return 0.5

    def _ragged(seq: list[float]) -> float:
        s = 0.0
        for a, b in zip(seq, seq[1:]):
            d = abs(a - b)
            s += d
            if d > max_step:
                s += (d - max_step) * 3.0
        return s

    for _ in range(max(0, int(passes))):
        improved = False
        seq = [_en(r) for r in items]
        for i in range(len(items) - 1):
            cur = _ragged(seq)
            seq[i], seq[i + 1] = seq[i + 1], seq[i]
            items[i], items[i + 1] = items[i + 1], items[i]
            if _ragged(seq) < cur - 1e-9:
                improved = True
            else:
                seq[i], seq[i + 1] = seq[i + 1], seq[i]
                items[i], items[i + 1] = items[i + 1], items[i]
        if not improved:
            break
    return items


def _smooth_energy_pass(items: list[dict], max_step: float = 0.18,
                        start_energy: float | None = None,
                        tun: dict | None = None) -> list[dict]:
    """Плавная раскладка по энергии + отчёт в лог.

    Раньше здесь стоял create_energy_wave в голом try/except pass — то есть
    при любой ошибке оркестрация молча пропадала, и по логам нельзя было понять,
    раскладывалась ли вообще волна. Теперь причина сбоя видна.

    start_energy — энергия трека, который сейчас играет. Без неё раскладка
    стартует от края шкалы и на стыке батчей получается скачок: только что
    играл энергичный трек, а мозг выдал спокойный.
    """
    if not items or len(items) < 3:
        return items
    try:
        from app.services.orchestrator import smooth_energy_order as _seo

        tun = tun or {}
        max_step = _tun_float(tun, 'playlists', 'smooth_max_step', max_step)
        _passes = _tun_int(tun, 'playlists', 'smooth_passes', 8)

        def _en_list(seq: list[dict]) -> list[float]:
            out = []
            for r in seq:
                try:
                    v = r.get("energy")
                    out.append(float(v) if v is not None else 0.5)
                except (TypeError, ValueError):
                    out.append(0.5)
            return out

        def _ragged(seq: list[float]) -> float:
            s = 0.0
            for a, b in zip(seq, seq[1:]):
                d = abs(a - b)
                s += d
                if d > max_step:
                    s += (d - max_step) * 3.0
            return s

        before_r = _ragged(_en_list(items))
        by_id = {str(r.get("track_id") or ""): r for r in items}
        wrapped = [dict(r, id=str(r.get("track_id") or "")) for r in items]
        ordered = _seo(wrapped, None, max_step=max_step,
                       passes=_passes, start_energy=start_energy)
        new = [by_id.get(str(w.get("id"))) for w in ordered]
        new = [r for r in new if r is not None]
        if len(new) != len(items):
            from app.core.logging import get_logger as _gl

            _gl("wave").warning("energy pass: lost tracks ({} -> {}), order untouched",
                                 len(items), len(new))
            return items
        after_r = _ragged(_en_list(new))
        from app.core.logging import get_logger as _gl

        _gl("wave").info("energy pass: raggedness {:.3f} -> {:.3} over {} tracks (start_energy={})",
                         before_r, after_r, len(new), start_energy)
        return new
    except Exception as e:
        from app.core.logging import get_logger as _gl

        _gl("wave").warning("energy pass failed, order untouched: {}", e)
        return items


def wave_limits(db, user_id: str | None = None) -> dict[str, int]:
    """Размеры пула/окна/сидов/добора из тюнинга.

    Раньше эти пять чисел были зашиты константами (2000/800/200/5/7), а
    кнопки в «Настройках → Волна» меняли значение, которое никто не читал.
    Теперь ручки живые; дефолты = прежние константы, поведение 1в1.
    """
    tun = _tun_all(db, user_id)
    return {
        "pool_limit": _tun_int(tun, 'playlists', 'pool_limit', 2000),
        "score_window": _tun_int(tun, 'playlists', 'score_window', 800),
        "collab_extra": _tun_int(tun, 'playlists', 'collab_extra', 200),
        "seed_limit": _tun_int(tun, 'playlists', 'seed_limit', 5),
        "fatigue_days": _tun_int(tun, 'repeats', 'fatigue_days', 7),
        "radio_cluster_pool": _tun_int(tun, 'playlists', 'radio_cluster_pool',
                                       300),
    }


def _smooth_keys_order(items: list[dict],
                       tun: dict | None = None) -> list[dict]:
    """Key-сглаживание соседей: пузырьковые свопы, улучшающие суммарную
    совместимость тональностей (квинтовый круг). Своп разрешён, только если
    не рвёт энергетическую дугу (|Δenergy| < key_energy_max, дефолт 0.15).
    Порядок множества не меняет — только локальный порядок соседей."""
    items = list(items)
    if len(items) < 2:
        return items
    tun = tun or {}
    _e_max = _tun_float(tun, 'playlists', 'key_energy_max', 0.15)
    _passes = _tun_int(tun, 'playlists', 'key_passes', 3)
    try:
        from app.services.orchestrator import key_compatibility as _kcs
    except Exception:
        return items

    def _en(r: dict) -> float:
        try:
            v = r.get("energy")
            return float(v) if v is not None else 0.5
        except (TypeError, ValueError):
            return 0.5

    def _tot(ts: list[dict]) -> float:
        s = 0.0
        for a, b in zip(ts, ts[1:]):
            try:
                s += float(_kcs(a.get("key"), a.get("scale"),
                                b.get("key"), b.get("scale")))
            except Exception:
                s += 0.5
        return s

    for _ in range(max(0, _passes)):
        improved = False
        for i in range(len(items) - 1):
            if abs(_en(items[i]) - _en(items[i + 1])) >= _e_max:
                continue
            cur = _tot(items)
            items[i], items[i + 1] = items[i + 1], items[i]
            if _tot(items) > cur + 1e-9:
                improved = True
            else:
                items[i], items[i + 1] = items[i + 1], items[i]
        if not improved:
            break
    return items


def _smooth_flow_order(items: list[dict], tun: dict | None = None,
                       timbre_map: dict[str, Any] | None = None) -> list[dict]:
    """Локальная перестановка соседей по СОВМЕСТИМОСТИ ПОТОКА.

    Дуга (`order_by_wave_arc`) задаёт ФОРМУ пачки — как играть. Этот проход
    задаёт СОДЕРЖАНИЕ стыков: соседние треки не должны шокировать темпом и
    тембром. Тональность сглаживается отдельно (`_smooth_keys_order`), а
    темп и тембр остались без ограничения.

    Критерий пары: близость темпа (BPM) + похожесть тембра (chroma+MFCC).
    Своп разрешён, только если он не рвёт дугу (|Δenergy| < flow_energy_max)
    и суммарно улучшает раскладку. Множество треков не меняется — только
    локальный порядок соседей.
    """
    items = list(items)
    if len(items) < 2:
        return items
    tun = tun or {}
    _w_tempo = _tun_float(tun, 'playlists', 'flow_tempo_weight', 0.3)
    _w_timbre = _tun_float(tun, 'playlists', 'flow_timbre_weight', 0.3)
    if _w_tempo <= 0 and _w_timbre <= 0:
        return items
    _e_max = _tun_float(tun, 'playlists', 'flow_energy_max', 0.15)
    _passes = _tun_int(tun, 'playlists', 'flow_passes', 3)
    _tw_win = float(_tun_int(tun, 'playlists', 'flow_tempo_window', 40))
    try:
        from app.services import flow as _flow
    except Exception:
        return items
    timbre_map = timbre_map or {}

    def _en(r: dict) -> float:
        try:
            v = r.get("energy")
            return float(v) if v is not None else 0.5
        except (TypeError, ValueError):
            return 0.5

    def _tempo(r: dict) -> float | None:
        try:
            v = r.get("tempo")
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _pair(a: dict, b: dict) -> float:
        s = 0.0
        if _w_tempo > 0:
            s += _w_tempo * _flow.tempo_compatibility(
                _tempo(a), _tempo(b), _tw_win)
        if _w_timbre > 0:
            s += _w_timbre * _flow.timbre_similarity(
                timbre_map.get(str(a.get("track_id") or "")),
                timbre_map.get(str(b.get("track_id") or "")))
        return s

    def _tot(ts: list[dict]) -> float:
        return sum(_pair(a, b) for a, b in zip(ts, ts[1:]))

    for _ in range(max(0, _passes)):
        improved = False
        for i in range(len(items) - 1):
            if abs(_en(items[i]) - _en(items[i + 1])) >= _e_max:
                continue
            cur = _tot(items)
            items[i], items[i + 1] = items[i + 1], items[i]
            if _tot(items) > cur + 1e-9:
                improved = True
            else:
                items[i], items[i + 1] = items[i + 1], items[i]
        if not improved:
            break
    return items


def _radio_seed_anchor(db, user_id: str) -> str | None:
    """Внутренний id трека-якоря радио (`wave_seeds`) или None.

    Радио-сид — это «волна по треку/артисту». Раньше он жил только в капсуле
    (`GET /api/wave/seed`) и в очереди одного запроса: как только очередь
    радио доигрывалась, волна возвращалась к сидам вкуса — стиль («как у
    Bodies») терялся, и «Боевое» снова становилось «быстро и мощно».
    Теперь сохранённый сид — якорь волны: он идёт первым сидом (центроид
    звука, k-NN-опоры, тембр, кластер) и добирает пул соседями.
    """
    try:
        from app.db.models import Track
        from app.db.models import WaveSeed

        s = db.get(WaveSeed, str(user_id))
        if s is None:
            return None
        if (s.kind or '') == 'track':
            tid = str(s.ref or '').strip()
            if not tid:
                return None
            if db.get(Track, tid) is not None:
                return tid
            row = db.query(Track.id).filter(Track.external_id == tid).first()
            return str(row[0]) if row else None
        # Артист: якорь — его самый играемый трек (иначе стиль не удержать).
        name = str(s.ref or '').strip()
        if not name:
            return None
        row = db.query(Track.id).filter(Track.artist_name == name).order_by(
            Track.play_count.desc().nullslast()).first()
        if row is None:
            try:
                from app.services.artist_names import norm_name

                want = norm_name(name)
                row = db.query(Track.id).filter(
                    func.lower(func.trim(Track.artist_name)) == want).order_by(
                    Track.play_count.desc().nullslast()).first()
            except Exception:
                row = None
        return str(row[0]) if row else None
    except Exception:
        return None


def _radio_cluster_pool(db, anchor_id: str, limit: int = 300) -> list[str]:
    """Соседи якоря по кластеру (та же «полка» библиотеки).

    Пул волны — 2000 СЛУЧАЙНЫХ треков из библиотеки. Настоящие соседи
    играющего трека в него почти не попадают, поэтому «в стиле трека» было
    невозможно: даже с якорем в сидах кандидаты оказывались случайными.
    Кластерные соседи — дешёвый и честный добор (общий признак по librosa).
    """
    if not anchor_id:
        return []
    try:
        from app.db.models import TrackCluster

        cid = db.query(TrackCluster.cluster_id).filter(
            TrackCluster.track_id == anchor_id).first()
        if not cid:
            return []
        rows = db.query(TrackCluster.track_id).filter(
            TrackCluster.cluster_id == cid[0]).limit(max(1, int(limit))).all()
        return [str(r[0]) for r in rows]
    except Exception:
        return []


def wave_continue(db, user_id: str, queue: list[str] | None = None,
                  current_track_id: str | None = None, count: int = 20,
                  settings: dict | None = None,
                  exclude_ids: list[str] | None = None,
                  recent_events: list[dict] | None = None,
                  ratings_delta: list[dict] | None = None) -> dict:
    """Главная функция: дельта -> сиды -> кандидаты -> скоринг -> следующие N."""
    from app.db.models import Track, TrackDislike

    settings = settings or {}
    count = max(1, min(100, int(count or 20)))

    # Тюнинг пула/сидов/усталости. КНОПКИ в «Настройках → Волна» были, а
    # значения не применялись: пул, окно скоринга, добор коллаборативных,
    # число сидов и окно усталости были зашиты константами. Теперь ручки
    # живые (дефолты = прежние константы, поведение 1в1).
    _lim = wave_limits(db, user_id)
    _pool_limit = _lim["pool_limit"]
    _score_win = _lim["score_window"]
    _collab_extra = _lim["collab_extra"]
    _seed_limit = _lim["seed_limit"]
    _fatigue_days = _lim["fatigue_days"]

    applied = apply_delta(db, user_id, ratings_delta, recent_events)

    # Сессионный дрейф (порт MoodDriftDetector): скипы подряд остужают волну,
    # скипнутое исключаем из кандидатов — только на этот запрос, в БД не пишем.
    drift = session_drift(db, recent_events, user_id)

    _raw_queue = list(queue or []) + list(exclude_ids or [])
    played = set(_resolve_ids(db, _raw_queue))
    if _raw_queue and len(played) < len(set(map(str, _raw_queue))):
        # Часть id очереди не резолвится в Track (внешние id без совпадения
        # по external_id/uuid): такие треки фильтр по id не прикроет —
        # дубли и возвраты возможны. Логируем для диагностики.
        try:
            from app.core.logging import get_logger as _gl2

            _gl2("wave").warning("wave queue resolve miss: {}/{} ids unresolved",
                                 len(set(map(str, _raw_queue))) - len(played),
                                 len(set(map(str, _raw_queue))))
        except Exception:
            pass
    played.update(drift.get("skip_ids") or [])
    if current_track_id:
        played.update(_resolve_ids(db, [current_track_id]))

    seeds = select_seeds(db, user_id,
                         characteristic=settings.get('characteristic'),
                         limit=_seed_limit)
    # Радио-якорь: сохранённый сид радио («волна по треку/артисту») держит
    # стиль. Раньше он в волне не участвовал вообще — очередь радио
    # доигрывалась, и волна возвращалась к сидам вкуса («быстро и мощно»
    # вместо «как у Bodies»). Якорь идёт первым сидом.
    _radio_anchor = _radio_seed_anchor(db, user_id)
    if _radio_anchor and _radio_anchor not in played:
        seeds = ([_radio_anchor] +
                 [s for s in seeds if s != _radio_anchor])[:_seed_limit]
    cur_ids: list[str] = []
    if current_track_id:
        cur = _resolve_ids(db, [current_track_id])
        cur_ids = cur
        seeds = (cur + seeds)[:_seed_limit]

    # Стартовое настроение — для плавного морфинга в целевое.
    # Целевое: settings.mood (выбор юзера на странице «Моя волна»).
    from app.db.models import TrackFeatures as _TF0

    target_mood = norm_mood(settings.get('mood'))
    start_mood: str | None = None
    ref_key: tuple | None = None
    ref_sentiment: str | None = None
    _cf = None
    if cur_ids:
        try:
            _cf = db.query(_TF0).filter(_TF0.track_id == cur_ids[0]).first()
            start_mood = _mood_of(_cf)
            if _cf is not None:
                ref_key = (getattr(_cf, "key_name", None),
                           getattr(_cf, "scale", None))
                try:
                    _mv = getattr(_cf, "mood_vector", None) or {}
                    ref_sentiment = str(_mv.get("ai_sentiment") or "").lower() or None
                except Exception:
                    ref_sentiment = None
        except Exception:
            start_mood = None
    target_activity = _norm_activity(settings.get('activity'))
    # Морф = мы меняем вайб на ходу. Раньше это ловилось только по смене муда
    # (start_mood != target_mood), и смена ОДНОГО занятия — «Засыпаю» вместо
    # «Тренировка» при той же пилюле настроения — морфом не считалась: пачка
    # оставалась большой, и новый контекст был слышен только через полчаса.
    # Теперь учитываем и занятие: если текущий трек не вписывается в его
    # жёсткие рамки (sleep = energy<=0.4, темп<=110) — это переход.
    morphing = bool(target_mood and start_mood and target_mood != start_mood)
    if not morphing and target_activity and _cf is not None:
        _hp = ACTIVITY_HARD.get(target_activity) or {}
        if _hp:
            try:
                _cur_en = float(_cf.energy) if _cf.energy is not None else 0.5
            except (TypeError, ValueError):
                _cur_en = 0.5
            try:
                _cur_bpm = float(_cf.tempo_bpm) if _cf.tempo_bpm else 0.0
            except (TypeError, ValueError):
                _cur_bpm = 0.0
            if ('energy_max' in _hp and _cur_en > _hp['energy_max']) or \
               ('energy_min' in _hp and _cur_en < _hp['energy_min']) or \
               ('tempo_max' in _hp and _cur_bpm and _cur_bpm > _hp['tempo_max']) or \
               ('tempo_min' in _hp and _cur_bpm and _cur_bpm < _hp['tempo_min']):
                morphing = True
    # Целевой сентимент лирики по муд-пресету (иначе держим текущий вайб).
    _POS_MOODS = {"energetic", "excited", "happy", "upbeat"}
    _NEG_MOODS = {"sad", "melancholic", "dark", "aggressive"}
    target_sentiment: str | None = None
    if target_mood:
        if target_mood in _POS_MOODS:
            target_sentiment = "positive"
        elif target_mood in _NEG_MOODS:
            target_sentiment = "negative"

    # Исключения: очередь + дизлайки + баны (имена ИЛИ Navidrome-ID).
    dis = {str(r.track_id) for r in
           db.query(TrackDislike).filter_by(user_id=user_id).all()}
    try:
        from app.services.artist_names import load_bans as _load_bans
        ban_names, ban_ids = _load_bans(db, user_id)
    except Exception:
        ban_names, ban_ids = set(), set()

    # collab-подмес: кто у похожих в топе — тем выше collabScore.
    # Считаем ДО формирования пула кандидатов: раньше это было ниже, и
    # коллаборативные треки часто просто не попадали в первые 2000 строк
    # выборки (limit без ORDER BY), то есть даже при идеально посчитанной
    # коллаборации её вывод не доходил до скора.
    collab_scores: dict[str, float] = {}
    try:
        from app.services import collab as _cb
        rec = _cb.recommend_for_user(db, user_id, n=200)
        for it in rec.get('items', []):
            collab_scores[str(it['track_id'])] = float(it.get('score', 0) or 0)
    except Exception as e:
        try:
            from app.core.logging import get_logger as _gl

            _gl("wave").warning("collab recommend failed: {}", e)
        except Exception:
            pass

    # Пул кандидатов: всё кроме сыгранного/дизлайков/банов, капом 2000.
    # Только то, что реально сыграет плеер. Локальные файлы (disk:/demo-) в
    # плейлист не попадают: у них нет Navidrome-id, поэтому при выгрузке они
    # молча выбрасываются и очередь на выходе оказывается короче запрошенного.
    # В демо-режиме фильтр выключается сам (playable.has_navidrome) — там диск
    # и есть единственная библиотека.
    #
    # ВАЖНО, почему тут ORDER BY random() (было: limit без порядка):
    # при limit без ORDER BY SQLite отдаёт ОДИН И ТОТ ЖЕ кусок при каждом
    # запросе. На библиотеке в 79k это 2000 из 79000 — всегда одни и те же
    # треки, остальные мозг не видел никогда и не увидит. Случайный порядок
    # даёт разную выборку на каждый вызов: библиотека изучается целиком, и
    # шанс, что понравившийся трек попадёт в выдачу, перестаёт зависеть от
    # того, где он лежит в базе.
    #
    # Сортировка идёт в БД (один проход, дешевле, чем тянуть 79k id в питон).
    # Подборка по вкусу — это _score_pool ниже; тут только честный вход.
    q = db.query(Track.id)
    try:
        from app.services import playable as _pl

        q = _pl.apply(q, db)
    except Exception as e:  # noqa: BLE001 — лучше лишний трек, чем пустая волна
        try:
            from app.core.logging import get_logger as _gl

            _gl("wave").warning("playable filter unavailable, using full pool: {}", e)
        except Exception:
            pass
    skip_n = len(played | dis)
    cand: list[str] = []
    # Соседи радио-якоря — В ПУЛ ПЕРВЫМИ. Пул волны набирается случайно,
    # и настоящие соседи играющего трека в него не попадали: «в стиле трека»
    # было невозможно даже с якорем в сидах. Кап radio_cluster_pool — волна
    # не должна схлопнуться в один кластер.
    _radio_pool: list[str] = []
    if _radio_anchor:
        try:
            _radio_pool = [t for t in _radio_cluster_pool(
                db, _radio_anchor, _lim["radio_cluster_pool"])
                if t not in played and t not in dis and t != _radio_anchor]
        except Exception:
            _radio_pool = []
    _rand_limit = max(1, _pool_limit - len(_radio_pool))
    cand = list(_radio_pool)
    try:
                cand += [str(r[0]) for r in
                         q.order_by(func.random()).limit(_rand_limit + skip_n).all()]

    except Exception as e:  # noqa: BLE001
        # Нет random() (старый SQLite/другая БД) — падаем на прежнее поведение
        # с честной пометкой в лог: это тихо ломает разнообразие выдачи.
        try:
            from app.core.logging import get_logger as _gl

            _gl("wave").warning(
                "random() unavailable ({}), пул снова фиксированный — "
                "на большой библиотеке это 1% треков без ротации", e)
        except Exception:
            pass
        cand += [str(r[0]) for r in q.limit(_rand_limit + skip_n).all()]
    cand = [c for c in cand if c not in played and c not in dis][:_pool_limit]

    # Порядок пула — ДЕТЕРМИНИРОВАННЫЙ. Пул выше собран через ORDER BY
    # random() (это нужно, чтобы мозг постепенно изучал всю библиотеку), но
    # дальше по пулу идут решения, которые зависят от ПОРЯДКА:
    #   * дедуп «та же песня под другим row id» — оставляет ПЕРВОГО;
    #   * штрафы за повтор артиста/жанра/настроения — считаются по обходу.
    # При случайном порядке побеждал случайный дубль, а одна и та же пилюля
    # давала разную выдачу от вызова к вызову. Порядок по id стабилен.
    cand = sorted(cand)
    # Гарантируем, что коллаборативные треки в пуле есть: если библиотека
    # больше 2000 и limit без ORDER BY отсёк нужные строки, добавляем их явно.
    # Без этого collab-вес просто не участвовал бы в скоре части треков.
    if collab_scores:
        _cset = set(cand)
        _missing = [t for t in collab_scores if t not in _cset and t not in played and t not in dis]
        if _missing:
            cand = cand + _missing[:_collab_extra]
            try:
                from app.core.logging import get_logger as _gl

                _gl("wave").info("collab: добавлено {} треков в пул сверх лимита", len(_missing[:_collab_extra]))
            except Exception:
                pass
    # Мета кандидатов — один проход чанками (SQLite держит ~999 vars в IN).
    # Нужна и для банов, и для вырезания «той же песни» под другим row id.
    meta: dict[str, Any] = {}
    if cand:
        for i in range(0, len(cand), 500):
            chunk = cand[i:i + 500]
            try:
                for t in db.query(Track).filter(Track.id.in_(chunk)).all():
                    meta[str(t.id)] = t
            except Exception:
                pass
    # Фичи кандидатов — одним проходом чанками. Нужны ДО дедупа: когда у
    # одной песни два row id, побеждает тот, у которого есть анализ. Иначе
    # дедуп оставлял непроанализированный дубль, а жёсткий mood-фильтр его
    # выкидывал — и песня выпадала из волны целиком.
    fm: dict[str, Any] = {}
    if cand:
        from app.db.models import TrackFeatures as _TF

        for i in range(0, len(cand[:_pool_limit]), 500):
            chunk = cand[i:i + 500]
            try:
                for f in db.query(_TF).filter(_TF.track_id.in_(chunk)).all():
                    fm[str(f.track_id)] = f
            except Exception:
                pass
    # Баны режем по мета (нужен artist) — батчем.
    # Сравнение через artist_names: бан «GASHI» ловит и трек
    # «Dark Polo Gang/GASHI/Capo Plaza», регистр не важен.
    # Плюс точный матч по Navidrome-ID (десктопные баны).
    if (ban_names or ban_ids) and cand:
        from app.services.artist_names import is_banned_either as _is_banned

        cand = [c for c in cand
                if not (meta.get(c) and _is_banned(
                    meta[c].artist_name,
                    getattr(meta[c], "artist_external_id", None),
                    ban_names, ban_ids))]
    # «Та же песня» под другим row id (дубль после перескана): по id-фильтр
    # её не ловит — получаем дубли в очереди и возврат задизлайканного.
    # Ключ — нормализованные артист+название (как в dedup.norm_text).
    # Сюда же — интра-дедуп: один ответ не содержит песню дважды.
    excluded_keys: set[tuple] = set()
    try:
        from app.services.dedup import norm_text as _norm_text

        def _song_key(artist: Any, title: Any) -> tuple | None:
            a, t = _norm_text(artist), _norm_text(title)
            return (a, t) if (a and t) else None

        _missing = [str(tid) for tid in
                    list(played) + list(dis) + list(drift.get("skip_ids") or [])
                    if str(tid) not in meta]
        for i in range(0, len(_missing), 500):
            try:
                for t in db.query(Track).filter(
                        Track.id.in_(_missing[i:i + 500])).all():
                    meta[str(t.id)] = t
            except Exception:
                pass
        for tid in list(played) + list(dis) + list(drift.get("skip_ids") or []):
            tm = meta.get(str(tid))
            if tm is not None:
                k = _song_key(tm.artist_name, tm.title)
                if k:
                    excluded_keys.add(k)
        if excluded_keys or cand:
            seen_keys: set[tuple] = set()
            deduped: list[str] = []
            # Победитель дубля — тот, у кого есть анализ (иначе его выкинет
            # жёсткий mood-фильтр и песня выпадет из волны). Порядок
            # детерминированный: пул уже отсортирован по id.
            _order = sorted(cand, key=lambda c: (0 if c in fm else 1, c))
            for c in _order:
                tm = meta.get(c)
                k = _song_key(tm.artist_name, tm.title) if tm is not None else None
                if k and (k in excluded_keys or k in seen_keys):
                    continue
                if k:
                    seen_keys.add(k)
                deduped.append(c)
            # Возвращаем дедуп в исходный (отсортированный) порядок пула —
            # иначе порядок выдачи снова зависит от того, кто выиграл дедуп.
            _pos = {c: i for i, c in enumerate(cand)}
            deduped.sort(key=lambda c: _pos.get(c, 0))
            if len(deduped) != len(cand):
                from app.core.logging import get_logger as _gl

                _gl("wave").info("wave song-key dedup: {} -> {} (queue/dis/skip)",
                                 len(cand), len(deduped))
            cand = deduped
    except Exception:
        pass
    # Немузыкальные треки (скиты/интерлюдии): разговорные вставки в волну
    # не берём — раздражают. Тумблер: Автоматизация → wave_skip_non_music.
    try:
        from app.services import automation as _auto

        _skip_nm = bool(_auto.get_flags(db).get("wave_skip_non_music", True))
    except Exception:
        _skip_nm = True
    if _skip_nm and cand:
        _before_nm = len(cand)
        cand = [c for c in cand
                if not (meta.get(c) and _is_non_music_track(meta[c].title))]
        if len(cand) != _before_nm:
            try:
                from app.core.logging import get_logger as _gl

                _gl("wave").info("wave non-music filter: {} -> {} (skit/interlude/intro)",
                                 _before_nm, len(cand))
            except Exception:
                pass
    # Временный бан жанра из дрейфа (3+ скипа жанра за сессию): только запрос.
    _tmp_genres = set(drift.get("temp_banned_genres") or [])
    if _tmp_genres and cand:
        try:
            _gm = {str(t.id): (t.genre or "") for t in
                   db.query(Track).filter(Track.id.in_(cand[:_pool_limit])).all()}
        except Exception:
            _gm = {}
        cand = [c for c in cand if _gm.get(c, "").lower() not in _tmp_genres]
    # Жёсткий mood-фильтр по семейству лейблов пресета.
    # При морфинге (старт -> цель) разрешаем оба настроения + безфичные,
    # градиент раскладываем после скоринга.
    #
    # ТРИ защиты, которых раньше не было:
    #  1) сравнение идёт в канонических кодах (_mood_codes_of) — код пилюли
    #     плеера 'calm' и муд-лейбл анализатора 'спокойный' теперь одно и то же;
    #  2) фильтр НЕ выкидывает треки без анализа (unknown = пропуск, как в
    #     фильтре языка) — иначе на непроанализированной библиотеке пул
    #     схлопывался бы в «всё, о чём мозг ничего не знает»;
    #  3) если после фильтра осталось меньше, чем нужно для пачки, фильтр
    #     откатывается целиком и пишется в лог. Пустая «Спокойная» волна
    #     хуже, чем неидеальная.
    mood = target_mood
    _act_code = target_activity
    _hard_act = ACTIVITY_HARD.get(_act_code or '', {})
    if (mood or _hard_act) and cand:
        # Фичи уже загружены выше (для дедупа) — второй запрос не делаем.
        try:
            _gm2 = {str(t.id): (t.genre or '') for t in
                    db.query(Track).filter(Track.id.in_(cand[:_pool_limit])).all()}
        except Exception:
            _gm2 = {}
        _want = set((MOOD_PRESETS.get(mood) or {}).get('moods') or set())
        if mood and morphing and start_mood:
            _want = _want | set((MOOD_PRESETS.get(start_mood) or {})
                                .get('moods') or set())
        # Числовая пилюля: центр в муд-пространстве и радиус.
        _mood_target = MOOD_TARGETS.get(mood or '') or None
        # Векторы пула: свой анализ, а для треков без анализа — средний
        # вектор их жанра (по проанализированным трекам этого жанра).
        # Трек без анализа не выкидывается: он получает оценку «примерно
        # так» и может попасть в волну, пока идёт анализ.
        # Векторы жанров: средний по проанализированным трекам жанра в пуле.
        # Нужен трекам без анализа: своего вектора у них нет, и без него они
        # вообще не могли соответствовать ни одной пилюле (а должны — пока
        # идёт анализ, они остаются в игре).
        _gvecs = genre_vector_map(fm, _gm2)
        # Муд жанра, выведенный из ДАННЫХ: муд большинства уже
        # проанализированных треков этого жанра внутри пула. Нужен, потому
        # что таблица жанр->настроение покрывает меньшинство жанров
        # (rap/hip-hop/pop/indie/lo-fi в неё не входили), и трек такого
        # жанра проходил ЛЮБУЮ пилюлю: фильтр его не отсекал, и в «Боевое»
        # попадал лоу-фай рэп. Считаем по пулу — это бесплатно, отдельного
        # запроса не нужно.
        _genre_auto: dict[str, str] = {}
        try:
            from collections import Counter as _Ctr

            _votes: dict[str, _Ctr] = {}
            for _c, _f in fm.items():
                _g = (_gm2.get(_c) or '').strip().lower()
                if not _g:
                    continue
                _codes = _mood_codes_of(_f)
                if not _codes:
                    continue
                _v = _votes.setdefault(_g, _Ctr())
                for _code in _codes:
                    _v[_code] += 1
            _genre_auto = {g: v.most_common(1)[0][0] for g, v in _votes.items() if v}
        except Exception:
            _genre_auto = {}

        def _passes(c: str) -> bool:
            f = fm.get(c)
            _preset = MOOD_PRESETS.get(mood) or {} if mood else {}
            return mood_filter_passes(
                f, _gm2.get(c), _want, _mood_target, MOOD_RADIUS,
                limits=(_preset, _hard_act), gvecs=_gvecs,
                genre_auto=_genre_auto)

        _before_mood = len(cand)
        _mood_backup = list(cand)
        cand = [c for c in cand if _passes(c)]
        _min_keep = max(10, int(count or 20))
        if len(cand) < _min_keep:
            try:
                from app.core.logging import get_logger as _gl

                _gl("wave").warning(
                    "context filter (mood={}, activity={}): {} из {} кандидатов — "
                    "ниже минимума {}, фильтр откачен (иначе волна пустеет)",
                    mood, _act_code, len(cand), _before_mood, _min_keep)
            except Exception:
                pass
            cand = _mood_backup
        elif len(cand) != _before_mood:
            try:
                from app.core.logging import get_logger as _gl

                _gl("wave").info("context filter (mood={}, activity={}): {} -> {} кандидатов",
                                 mood, _act_code, _before_mood, len(cand))
            except Exception:
                pass

    # Фильтр по языку (пилюли клиента «по языку»): ru / foreign / instrumental.
    # Язык берём из Lyrics.language (детект при скачивании текстов).
    # Треки без текстов НЕ выкидываем (unknown = пропуск) — иначе пустая очередь,
    # кроме опционального settings.language_strict=true (только доказанные).
    # Кириллический fast-path (_meta_ru_ids): трек без текстов, но с кириллицей
    # в метаданных — точно русский: держится в 'ru', выкидывается из 'foreign'
    # и 'instrumental'. Латиница без текстов — по-прежнему unknown=pass.
    lang = _norm_language(settings.get('language'))
    strict = bool(settings.get('language_strict'))
    if lang and cand:
        from app.db.models import Lyrics as _Ly

        try:
            from app.services.lyrics import detect_lyrics_language as _det_lang

            _lm: dict[str, str] = {}
            for r in db.query(_Ly).filter(_Ly.track_id.in_(cand[:_pool_limit])).all():
                _lang_v = (r.language or '').strip().lower()
                if not _lang_v:
                    try:
                        _lang_v = _det_lang(getattr(r, 'text', None))
                    except Exception:
                        _lang_v = ''
                _lm[str(r.track_id)] = _lang_v
        except Exception:
            _lm = {}
        _meta_ru: set = set()
        if lang in ('foreign', 'instrumental') or strict:
            try:
                _meta_ru = _meta_ru_ids(db, [c for c in cand[:_pool_limit] if c not in _lm])
            except Exception:
                _meta_ru = set()
        if lang == 'ru':
            if strict:
                cand = [c for c in cand
                        if (_lm.get(c) or '') == 'ru' or c in _meta_ru]
            else:
                cand = [c for c in cand
                        if c not in _lm or (_lm.get(c) or '') == 'ru']
        elif lang == 'foreign':
            if strict:
                cand = [c for c in cand if (_lm.get(c) or '') == 'foreign']
            else:
                cand = [c for c in cand
                        if (_lm.get(c) or '') != 'ru' and c not in _meta_ru]
        elif lang == 'instrumental':
            if strict:
                cand = [c for c in cand if (_lm.get(c) or '') == 'instrumental']
            else:
                cand = [c for c in cand
                        if (c not in _lm or (_lm.get(c) or '') == 'instrumental')
                        and c not in _meta_ru]

    # collab-подмес считается выше по коду (до формирования пула кандидатов),
    # чтобы его треки гарантированно попадали в скоренное окно.

    # recent_events для behaviorBonus — нормализуем id к нашим
    norm_events: list[dict] = []
    for e in (recent_events or []):
        if not isinstance(e, dict):
            continue
        tids = _resolve_ids(db, [str(e.get('track_id') or '')])
        if tids:
            norm_events.append({'track_id': tids[0], 'action': e.get('action'),
                               'position_sec': e.get('position_sec')})

    # KMeans-кластеры сидов: кандидаты с той же «полки» библиотеки —
    # точнее попадание (бонус в скоринге). Нет кластеров — тихо пропускаем.
    cluster_map: dict[str, int] = {}
    seed_clusters: set[int] = set()
    try:
        from app.db.models import TrackCluster as _TC

        _need_cl = list({str(c) for c in (cand[:_score_win] + seeds)})
        for i in range(0, len(_need_cl), 500):
            try:
                for row in db.query(_TC).filter(
                        _TC.algorithm == "kmeans",
                        _TC.track_id.in_(_need_cl[i:i + 500])).all():
                    cluster_map[str(row.track_id)] = int(row.cluster_id)
            except Exception:
                pass
        seed_clusters = {cluster_map[s] for s in seeds if s in cluster_map}
    except Exception:
        cluster_map, seed_clusters = {}, set()

    # Сессионный fingerprint: что играет/доиграно прямо сейчас (порт mobile
    # rolling fingerprint) — волна подстраивается под текущий заход.
    session_ids: list[str] = list(cur_ids)
    for e in norm_events:
        if str(e.get("action") or "") in ("play", "complete", "replay", "like"):
            _sid = str(e.get("track_id") or "")
            if _sid and _sid not in session_ids:
                session_ids.append(_sid)
    session_ids = session_ids[:20]
    # Негатив: дизлайки (топ-500 свежих) — похожее штрафуем вектором.
    neg_ids: list[str] = []
    try:
        neg_ids = [str(r.track_id) for r in
                   db.query(TrackDislike).filter_by(user_id=user_id)
                   .order_by(TrackDislike.created_at.desc()).limit(500).all()]
    except Exception:
        neg_ids = list(dis)[:500]
    # Recency: последнее прослушивание кандидатов (затухание новизны).
    last_played: dict[str, Any] = {}
    try:
        from datetime import timedelta as _td

        from app.db.models import PlayHistory as _PH

        _lp_ids = list(dict.fromkeys(list(cand[:_score_win]) + list(played)))[:1200]
        for i in range(0, len(_lp_ids), 500):
            try:
                for tid, when in db.query(_PH.track_id, _PH.played_at).filter(
                        _PH.user_id == user_id,
                        _PH.track_id.in_(_lp_ids[i:i + 500])).all():
                    if when and (str(tid) not in last_played or
                                 when > last_played[str(tid)]):
                        last_played[str(tid)] = when
            except Exception:
                pass
    except Exception:
        pass
    # Усталость артиста: сколько раз гоняли за 7 дней.
    fatigue: dict[str, int] = {}
    try:
        from datetime import datetime as _dt2
        from datetime import timedelta as _td2

        from app.db.models import PlayHistory as _PH2

        _since = _dt2.utcnow() - _td2(days=_fatigue_days)
        _recent: list[str] = []
        try:
            _recent = [str(r[0]) for r in
                       db.query(_PH2.track_id).filter(
                           _PH2.user_id == user_id,
                           _PH2.played_at >= _since)
                       .order_by(_PH2.played_at.desc()).limit(5000).all()]
        except Exception:
            _recent = []
        if _recent:
            _amap: dict[str, str] = {}
            _uniq = list(dict.fromkeys(_recent))
            for i in range(0, len(_uniq), 500):
                try:
                    for t in db.query(Track).filter(
                            Track.id.in_(_uniq[i:i + 500])).all():
                        if t.artist_name:
                            _amap[str(t.id)] = t.artist_name
                except Exception:
                    pass
            _cnt: dict[str, int] = {}
            for tid in _recent:
                _an = _amap.get(tid)
                if _an:
                    _cnt[_an] = _cnt.get(_an, 0) + 1
            fatigue = _cnt
    except Exception:
        pass

    # Джиттер — от содержимого очереди, а не только её длины: иначе две
    # разные очереди одной длины дают одинаковый порядок (повторы хит-парада).
    try:
        import hashlib as _hl

        _pq = _hl.blake2s(",".join(sorted(played)).encode(),
                          digest_size=8).hexdigest()
    except Exception:
        _pq = str(len(played))
    # Час и контекст: «часто в этот час» + руки бандита + ассоциации.
    try:
        from app.core.time import local_hour as _local_hour

        _cur_hour = _local_hour()
    except Exception:
        _cur_hour = datetime.now().hour
    try:
        from app.core.time import server_now as _server_now
        from app.services.taste import context_of as _ctx_of

        _dow = _server_now().isoweekday() % 7
        _ctx = _ctx_of(_cur_hour, _dow)
    except Exception:
        _ctx = "day_wd"
    time_map = time_bonus_for(db, user_id, cand[:_score_win], _cur_hour)
    arm_artist, arm_genre = arm_boost_for(db, user_id, _ctx)
    try:
        mood_arm_map = mood_boost_for(db, user_id, _ctx)
    except Exception:
        mood_arm_map = {}
    assoc = session_assoc(db, user_id, seeds)
    # CLAP-карта пула+сидов+сессии: нет анализа → {} → вклад 0, не ломается.
    try:
        _clap_ids = list(dict.fromkeys(
            list(cand[:_score_win]) + list(seeds) + list(session_ids)))[:1500]
        clap_map = _load_clap_map(db, _clap_ids)
    except Exception:
        clap_map = {}
    # Энергия текущего трека — якорь скип-риска (резкий скачок = риск).
    _cur_en2: float | None = None
    try:
        if _cf is not None and getattr(_cf, "energy", None) is not None:
            _cur_en2 = float(_cf.energy)
    except (TypeError, ValueError):
        _cur_en2 = None
    # Авто-настроение: ручная пилюля побеждает всегда; авто — только мягкий
    # ctx-бонус в скоринге, жёсткий mood-фильтр выше уже отработал по ручной.
    _manual_mood = norm_mood(settings.get('mood'))
    _auto_mood: str | None = None
    try:
        if not _manual_mood:
            _auto_mood = infer_auto_mood(db, user_id, session_ids, _cur_hour)
    except Exception:
        _auto_mood = None
    settings_eff = dict(settings)
    mood_source = "manual" if _manual_mood else ("auto" if _auto_mood else "none")
    if mood_source == "auto" and _auto_mood:
        settings_eff["mood"] = _auto_mood
    # Целевой сентимент под эффективное настроение (для lyr_c).
    _eff_mood_norm = None
    try:
        _eff_mood_norm = _norm_mood(settings_eff.get("mood"))
    except Exception:
        _eff_mood_norm = None
    target_sentiment_eff = target_sentiment
    try:
        if _eff_mood_norm and not _manual_mood:
            if _eff_mood_norm in _POS_MOODS:
                target_sentiment_eff = "positive"
            elif _eff_mood_norm in _NEG_MOODS:
                target_sentiment_eff = "negative"
    except Exception:
        pass
    # Скоренное окно: обычный пул режем 800, но коллаборативные треки,
    # добавленные в cand сверх лимита, в это окно не попали бы — и их вклад
    # в скор снова исчез бы. Поэтому берём обычные 800 и сверху добавляем
    # сами коллаборативные (их не больше 200, всего ~1000 строк на скоринг).
    _score_pool = cand[:_score_win]
    if collab_scores:
        _in_pool = set(_score_pool)
        _extra = [t for t in collab_scores if t in cand and t not in _in_pool]
        if _extra:
            _score_pool = _score_pool + _extra[:_collab_extra]
    # Порядок обхода пула — ДЕТЕРМИНИРОВАННЫЙ (по id). Пул выше собран
    # через ORDER BY random() (это нужно, чтобы мозг постепенно изучал всю
    # библиотеку), но скоринг идёт по нему в порядке выборки, а штрафы за
    # повтор артиста/жанра/настроения считаются именно по порядку обхода.
    # Из-за случайного порядка штраф получал СЛУЧАЙНЫЙ трек из пары, и одна
    # и та же пилюля давала разную выдачу от запроса к запросу. Теперь
    # порядок стабилен — разнообразие обеспечивает сам джиттер (seeded).
    _score_pool = sorted(_score_pool)
    ranked = score_candidates(db, user_id, _score_pool, seeds, settings_eff,
                              norm_events, collab_scores,
                              current_hour=_cur_hour,
                              jitter_seed=f"{user_id}:{len(played)}:{_pq}",
                              drift=drift, ref_key=ref_key,
                              cluster_map=cluster_map or None,
                              seed_clusters=seed_clusters or None,
                              ref_sentiment=ref_sentiment,
                              target_sentiment=target_sentiment_eff,
                              session_ids=session_ids or None,
                              neg_ids=neg_ids or None,
                              last_played=last_played or None,
                              fatigue=fatigue or None,
                              time_map=time_map or None,
                              arm_artist=arm_artist or None,
                              arm_genre=arm_genre or None,
                              assoc=assoc or None,
                              clap_map=clap_map or None,
                              clap_weight=None,
                              mood_arm=mood_arm_map or None,
                              cur_energy=_cur_en2)
    # Недавнее реже (новизна как у cold-start novelty=True): уже учтено
    # novelty-членом, дубли очереди на всякий случай режем ещё раз
    ranked = [r for r in ranked if r['track_id'] not in played]
    # Адаптивная пачка: при смене настроения — меньше, чтобы новый вайб
    # было слышно через 4-5 треков, а не через 10. Капы — личные.
    _sel_tun = _tun_all(db, user_id)
    eff_count, adapt_reason = adaptive_count(count, drift, morphing, _sel_tun)
    # Разнообразие пачки. Раньше брался просто топ по скору, и в него
    # могли попасть 8 треков одного артиста или одного жанра: волна
    # звучала монотонно, даже если скоринг был «правильным». Теперь капы
    # артиста/жанра/настроения + разброс по муд-пространству.
    _a_cap = _tun_int(_sel_tun, 'character', 'wave_artist_cap', 2)
    _g_cap = _tun_int(_sel_tun, 'character', 'wave_genre_cap', 0)
    # Кап кода настроения — только когда пилюля НЕ выбрана. С пилюлей одно
    # настроение в пачке это замысел пользователя, а не монотония.
    _m_cap = (_tun_int(_sel_tun, 'character', 'wave_mood_cap', 0)
              if not _eff_mood_norm else None)
    _spread = _tun_float(_sel_tun, 'character', 'wave_spread_min', 0.0)
    # Для разброса берём ТОЛЬКО собственные векторы (реальный анализ):
    # выведенный по жанру вектор одинаков для всех треков жанра, и по нему
    # второй трек того же жанра выглядел бы «дублем» первого.
    _vecs_own = {tid: mood_vector(f) for tid, f in fm.items()
                 if f is not None}
    _vecs_all = _wave_vectors(fm, meta)
    top = select_diverse(ranked, eff_count, artist_cap=_a_cap,
                         genre_cap=_g_cap, mood_cap=_m_cap,
                         vec_map=_vecs_own, spread_min=_spread)
    # Финальная страховка на ответе: id + баны артистов + «та же песня».
    # Ловит всё, что просочилось через срез пула (cand[:_score_win]/топ).
    if top and (dis or ban_names):
        try:
            from app.services.artist_names import is_banned as _is_banned2
            from app.services.dedup import norm_text as _norm_text2

            def _sk2(artist: Any, title: Any) -> tuple | None:
                a, t = _norm_text2(artist), _norm_text2(title)
                return (a, t) if (a and t) else None

            _safe: list[dict] = []
            for r in top:
                tid = str(r.get("track_id") or "")
                if tid in played or tid in dis:
                    continue
                if _is_banned2(r.get("artist_name"), ban_names):
                    continue
                if _sk2(r.get("artist_name"), r.get("title")) in excluded_keys:
                    continue
                _safe.append(r)
            if len(_safe) != len(top):
                from app.core.logging import get_logger as _gl3

                _gl3("wave").warning("wave final net dropped {} tracks (ban/dis/dupe)",
                                     len(top) - len(_safe))
            top = _safe
        except Exception:
            pass
    # Обогащаем ответ настроением/энергией/обложкой для страницы «Моя волна».
    # Без отдельных запросов за треками.
    try:
        from app.db.models import Track as _T
        from app.db.models import TrackFeatures as _TF
        from app.services.covers import resolve_track_cover_id as _resolve_cover

        _ids = [str(r.get("track_id") or "") for r in top if r.get("track_id")]
        _fm = {str(f.track_id): f for f in
               db.query(_TF).filter(_TF.track_id.in_(_ids)).all()} if _ids else {}
        _tm = {str(t.id): t for t in
               db.query(_T).filter(_T.id.in_(_ids)).all()} if _ids else {}
        for r in top:
            f = _fm.get(str(r.get("track_id") or ""))
            try:
                moods = list(f.mood_labels or []) if f is not None else []
            except Exception:
                moods = []
            r["mood"] = str(moods[0]).lower() if moods else None
            r["moods"] = [str(m).lower() for m in moods[:3]]
            # Канонический код настроения — для СРАВНЕНИЙ (градиент морфа,
            # мостик). Раньше сравнивали сырой лейбл ('спокойный') с
            # каноническим кодом ('calm') — сравнение всегда было False,
            # обе группы градиента пусты, и «голова — стартовое, хвост —
            # целевое» не строилось НИ РАЗУ, хотя API рапортовал morph.
            try:
                r["mood_code"] = _mood_of(f)
            except Exception:
                r["mood_code"] = None
            try:
                r["energy"] = float(f.energy) if f is not None and f.energy is not None else None
            except (TypeError, ValueError):
                r["energy"] = None
            # Остальные оси муд-пространства — чтобы клиент видел, где
            # трек стоит относительно выбранной пилюли.
            for _axis, _attr in (("valence", "valence"), ("arousal", "arousal"),
                                 ("dance", "danceability")):
                try:
                    r[_axis] = (float(getattr(f, _attr))
                                if f is not None and getattr(f, _attr, None) is not None
                                else None)
                except (TypeError, ValueError):
                    r[_axis] = None
            try:
                r["tempo"] = float(f.tempo_bpm) if f is not None and f.tempo_bpm else None
            except (TypeError, ValueError):
                r["tempo"] = None
            try:
                r["key"] = getattr(f, "key_name", None) if f is not None else None
                r["scale"] = getattr(f, "scale", None) if f is not None else None
            except Exception:
                r["key"] = r["scale"] = None
            try:
                t = _tm.get(str(r.get("track_id") or ""))
                r["cover_art_id"] = _resolve_cover(db, t) if t is not None else None
            except Exception:
                r["cover_art_id"] = None
    except Exception:
        pass
    # Тембровые векторы пачки — для попарного ограничения потока (соседние
    # треки не должны шокировать тембром). Считаем только для отобранных.
    _timbre_map: dict[str, Any] = {}
    try:
        from app.services import flow as _flow2

        for _r in top:
            _tid = str(_r.get("track_id") or "")
            _f = fm.get(_tid)
            if _f is None:
                continue
            _tv = _flow2.timbre_vector(_f)
            if _tv:
                _timbre_map[_tid] = _tv
    except Exception:
        _timbre_map = {}
    morph = None
    # Энергия текущего трека — якорь для плавной раскладки (см. _smooth_energy_pass).
    _cur_en: float | None = None
    if _cf is not None:
        try:
            if _cf.energy is not None:
                _cur_en = float(_cf.energy)
        except (TypeError, ValueError):
            _cur_en = None
    if morphing and top:
        # Переход по муд-пространству: ЛЕСТНИЦА от звука текущего трека к
        # центру целевой пилюли. Раньше градиент был бинарным («голова —
        # стартовое настроение, хвост — целевое»), и трека «наполовину
        # грустный, наполовину поднимающийся» не существовало: переход был
        # скачком между двумя состояниями.
        _mtun = _tun_all(db, user_id)
        _sv = mood_vector(_cf) if _cf is not None else {}
        if not _sv and start_mood:
            _sv = dict(MOOD_TARGETS.get(start_mood) or {})
        _tv = MOOD_TARGETS.get(target_mood or '') or {}
        if _sv and _tv:
            top = order_by_mood_ramp(top, _sv, _tv, _vecs_all)
            top = _smooth_ramp_order(top, _sv, _tv, _vecs_all)
            # Глобальная сортировка по энергии тут НЕ годится: она ломала
            # лестницу перехода (valence шёл своей дорогой). Поэтому
            # энергия сглаживается локально, порядок лестницы сохраняется.
            top = _smooth_energy_local(top, max_step=0.18)
            top = _smooth_keys_order(top, _mtun)
            top = _smooth_flow_order(top, _mtun, timbre_map=_timbre_map)
            morph = {"from": start_mood, "to": target_mood, "ramp": True}
        else:
            # Векторов нет (нет анализа у текущего трека и у цели) —
            # старый бинарный градиент по кодам настроения.
            g_start = [r for r in top
                       if (r.get("mood_code") or None) == start_mood]
            g_target = [r for r in top
                        if (r.get("mood_code") or None) == target_mood]
            head = _smooth_keys_order(g_start[:3], _mtun)
            tail = _smooth_keys_order(g_target[:4], _mtun)
            used = {str(r.get("track_id")) for r in head + tail}
            mid_n = max(0, len(top) - len(head) - len(tail))
            mid = _smooth_keys_order(
                [r for r in top if str(r.get("track_id")) not in used][:mid_n],
                _mtun)
            top = head + mid + tail
            morph = {"from": start_mood, "to": target_mood}
            # Раньше здесь энергетическая дуга не применялась вовсе: ветка
            # morph шла вместо неё, то есть при ВЫБРАННОМ настроении (главный
            # сценарий) переходов по энергии не было. Теперь дуга есть в обеих
            # ветках.
            top = _smooth_energy_pass(top, start_energy=_cur_en, tun=_mtun)
            top = _smooth_flow_order(top, _mtun, timbre_map=_timbre_map)
    elif len(top) > 3:
        # Форма волны: разгон -> вершина -> спад.
        # Раньше здесь была монотонная раскладка по энергии (smooth_energy_order):
        # сначала тихо, потом громко — и до самого конца. Волна без формы —
        # это «плейлист с плавными переходами». Теперь пачка имеет форму, а
        # точка входа — звук текущего трека, поэтому стык пачек не рвётся.
        _mtun = _tun_all(db, user_id)
        _arc = None
        if _tun_int(_mtun, 'playlists', 'arc_on', 1):
            _sv = mood_vector(_cf) if _cf is not None else {}
            _pv = (MOOD_TARGETS.get(_eff_mood_norm) or {}) if _eff_mood_norm else {}
            _arc = order_by_wave_arc(
                top, _vecs_all, start_vec=_sv, peak_vec=_pv,
                release=_tun_float(_mtun, 'playlists', 'arc_release',
                                   ARC_RELEASE),
                peak_pos=_tun_float(_mtun, 'playlists', 'arc_peak_pos',
                                    ARC_PEAK_POS))
        if _arc is not None:
            top = _arc
            try:
                from app.core.logging import get_logger as _gl

                _ens = [r.get("energy") for r in top if r.get("energy") is not None]
                if _ens:
                    _gl("wave").info(
                        "wave arc: {} треков, энергия {:.2f} -> {:.2f} -> {:.2f} "
                        "(вершина на {} %)", len(top), _ens[0],
                        max(_ens), _ens[-1],
                        int(100 * ARC_PEAK_POS))
            except Exception:
                pass
        else:
            # Дугу строить не из чего (нет анализа, пул однородный) —
            # старая раскладка по энергии, волна не должна пустеть.
            top = _smooth_energy_pass(top, start_energy=_cur_en, tun=_mtun)
        top = _smooth_keys_order(top, _mtun)
        top = _smooth_flow_order(top, _mtun, timbre_map=_timbre_map)
    # Мостик: первый трек выдачи — плавное продолжение текущего.
    # Если переход резкий — подтягиваем лучший мостик из топ-10 окна.
    if cur_ids and top and _cf is not None:
        try:
            from app.services.orchestrator import key_compatibility as _kcb

            try:
                _cur_en = float(_cf.energy) if _cf is not None and \
                    _cf.energy is not None else 0.5
            except (TypeError, ValueError):
                _cur_en = 0.5

            def _bridge(r: dict) -> float:
                try:
                    _de = abs(float(r.get("energy")
                                    if r.get("energy") is not None else 0.5) - _cur_en)
                except (TypeError, ValueError):
                    _de = 0.5
                try:
                    _kk = float(_kcb(ref_key[0] if ref_key else None,
                                      ref_key[1] if ref_key else None,
                                      r.get("key"), r.get("scale")))
                except Exception:
                    _kk = 0.5
                _mm = 1.0 if (r.get("mood_code") and start_mood and
                              r.get("mood_code") == start_mood) else 0.5
                return _kk * 0.5 + (1.0 - min(_de, 1.0)) * 0.3 + _mm * 0.2

            def _en_of(r: dict) -> float:
                try:
                    return float(r.get("energy")
                                 if r.get("energy") is not None else 0.5)
                except (TypeError, ValueError):
                    return 0.5

            _b0 = _bridge(top[0])
            _bi, _bv = 0, _b0
            # Мостик не должен ломать форму волны: в начало пачки подтягивается
            # трек того же динамического уровня (иначе разгон начинался бы
            # скачком). Поэтому кандидаты с заметно другой энергией отсекаем.
            _e_first = _en_of(top[0])
            for _i in range(1, min(10, len(top))):
                if abs(_en_of(top[_i]) - _e_first) > 0.12:
                    continue
                _s = _bridge(top[_i])
                if _s > _bv:
                    _bi, _bv = _i, _s
            if _bi and _bv > _b0 + 0.05:
                top[0], top[_bi] = top[_bi], top[0]
        except Exception:
            pass
    # Растяжка скоров окна: сырые total упираются в кламп 1.0 и весь топ
    # выглядит как «1.00, 1.00, …». Монотонно — порядок не трогаем.
    spread_scores(top)
    # Метрики волны: фиксируем, что эти треки реально показаны, с их score и
    # причиной. Без этого в истории не отличить «мозг предложил» от «юзер сам
    # поставил», и улучшать скоринг приходится вслепую.
    try:
        from app.services import rec_feedback as _rfb

        _rfb.mark_served(db, user_id, top, source="wave")
    except Exception as e:
        try:
            from app.core.logging import get_logger as _gl

            _gl("wave").warning("rec_feedback mark_served failed: {}", e)
        except Exception:
            pass
    # Целевая точка пилюли в муд-пространстве (если пилюля выбрана) —
    # плеер и разборы видят, ЧТО именно означает выбранное настроение.
    _eff_target = MOOD_TARGETS.get(_eff_mood_norm or '') or None
    return {'tracks': top, 'seeds': seeds,
            'applied': applied,
            'current_mood': start_mood,
            'morph': morph,
            'count_requested': count,
            'count_effective': eff_count,
            'adaptive': adapt_reason,
            'auto_mood': _auto_mood,
            'mood_source': mood_source,
            'mood_effective': settings_eff.get('mood') or None,
            'mood_target': _eff_target,
            'mood_radius': MOOD_RADIUS,
            'drift': {k: drift.get(k) for k in
                      ("severity", "consecutive_skips", "temp_banned_genres",
                       "warmth", "positive_streak")}
            if (drift.get("severity") or drift.get("warmth")) else None,
            'profile_version': datetime.utcnow().isoformat() + 'Z'}
