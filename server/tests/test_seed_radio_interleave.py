"""Перетасовка радио по артистам: сид-артист первым, без залипания."""
import os

os.environ.setdefault("REDIS_HOST", "localhost")

from app.services.seed_radio import _interleave_by_artist


def _mk(spec):
    """{'A': ['a1','a2','a3'], 'B': ['b1','b2']} -> (ids, artist_of)."""
    ids, amap = [], {}
    for a, ts in spec.items():
        for t in ts:
            ids.append(t)
            amap[t] = a
    return ids, amap


def test_seed_artist_goes_first():
    ids, amap = _mk({"S": ["s1", "s2"], "A": ["a1"], "B": ["b1"]})
    out = _interleave_by_artist(ids, amap, seed_artist="S", n=4, cap=2)
    assert out[0] == "s1", out


def test_seed_first_even_if_similar_scores_higher():
    """Похожие в исходном порядке идут первыми — сид всё равно обязан быть впереди."""
    ids, amap = _mk({"A": ["a1", "a2"], "B": ["b1"], "S": ["s1"]})
    out = _interleave_by_artist(ids, amap, seed_artist="S", n=4, cap=3)
    assert out[0] == "s1", out


def test_per_artist_cap_respected():
    ids, amap = _mk({"S": ["s1", "s2", "s3"], "A": ["a1", "a2", "a3"],
                     "B": ["b1", "b2", "b3"]})
    out = _interleave_by_artist(ids, amap, seed_artist="S", n=9, cap=2)
    counts = {}
    for t in out:
        counts[amap[t]] = counts.get(amap[t], 0) + 1
    assert max(counts.values()) <= 2, counts


def test_many_artists_instead_of_few():
    """Главный симптом: раньше выходило 2-3 артиста. Должно быть много."""
    spec = {"S": [f"s{i}" for i in range(3)]}
    for k in "ABCDEFGH":
        spec[k] = [f"{k.lower()}{i}" for i in range(5)]
    ids, amap = _mk(spec)
    out = _interleave_by_artist(ids, amap, seed_artist="S", n=24, cap=3)
    artists = {amap[t] for t in out}
    assert len(artists) >= 8, artists


def test_no_tracks_lost():
    ids, amap = _mk({"S": ["s1", "s2"], "A": ["a1", "a2", "a3"], "B": ["b1"]})
    out = _interleave_by_artist(ids, amap, seed_artist="S", n=6, cap=2)
    assert len(out) == len(ids), (out, ids)
    assert sorted(out) == sorted(ids)


def test_cap_relaxes_when_pool_thin():
    """Мало артистов и жёсткий кап = не отдаём короткую очередь."""
    ids, amap = _mk({"S": ["s1", "s2", "s3", "s4"], "A": ["a1", "a2"]})
    out = _interleave_by_artist(ids, amap, seed_artist="S", n=6, cap=1)
    assert len(out) == 6, out


def test_unknown_artist_bucket_not_dropped():
    ids = ["x1", "x2", "y1"]
    amap = {"x1": "A", "y1": "B"}          # x2 без артиста
    out = _interleave_by_artist(ids, amap, seed_artist="A", n=3, cap=5)
    assert len(out) == 3 and "x2" in out


def test_seed_case_insensitive():
    ids, amap = _mk({"s": ["s1"], "A": ["a1"]})
    out = _interleave_by_artist(ids, amap, seed_artist="S", n=2, cap=1)
    assert out[0] == "s1", out


def test_empty():
    assert _interleave_by_artist([], {}, seed_artist="X", n=10, cap=2) == []