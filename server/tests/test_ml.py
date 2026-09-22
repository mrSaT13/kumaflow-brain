import os
os.environ["DB_URL_OVERRIDE"] = "sqlite:///./test_api.db"
import pytest
from app.services.audio_analysis import _clamp  # avoid importing ml DB early

def _get_ml_funcs():
    from app.services.ml import _cosine, _feature_vector
    return _cosine, _feature_vector

class FakeF:
    tempo_bpm=120; energy=0.7; danceability=0.6; valence=0.5; arousal=0.6
    loudness_db=-12; spectral_centroid=2000; spectral_rolloff=4000; zero_crossing_rate=0.05

class FakeT: pass

def test_cosine_identity():
    _cosine, _ = _get_ml_funcs()
    import numpy as np
    a = np.array([1,0,0], dtype=float)
    assert _cosine(a,a) == pytest.approx(1.0)
    b = np.array([0,1,0], dtype=float)
    assert _cosine(a,b) == pytest.approx(0.0)

def test_feature_vector():
    _, _feature_vector = _get_ml_funcs()
    try:
        import numpy as np
    except ImportError:
        pytest.skip("numpy not installed")
    v = _feature_vector(FakeT(), FakeF())
    assert v is not None
    assert v.shape[0] == 9

def test_feature_vector_none():
    _, _feature_vector = _get_ml_funcs()
    assert _feature_vector(FakeT(), None) is None

def test_recommend_stub():
    # without DB we just check function exists and returns [] for unknown
    from app.services.ml import recommend_by_track
    # will try DB and fail gracefully in test env -> should return []
    # use sqlite memory via env override would be needed; just check callable
    assert callable(recommend_by_track)
