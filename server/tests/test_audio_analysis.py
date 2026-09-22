import pytest
from app.services.audio_analysis import estimate_key, mood_from_features, _clamp, resolve_local_file
from pathlib import Path

def test_clamp():
    assert _clamp(2) == 1.0
    assert _clamp(-1) == 0.0
    assert _clamp(0.5) == 0.5

def test_estimate_key_c_major():
    # хром = профиль мажора без сдвига -> C major
    chroma = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
    k, mode = estimate_key(chroma)
    assert k == "C"
    assert mode == "major"

def test_estimate_key_a_minor():
    chroma = [6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17]
    # сдвиг 9 = A (NOTE_NAMES[9]=A) minor peak
    k, mode = estimate_key(chroma)
    assert mode == "minor"

def test_mood_from_features():
    vec, labels = mood_from_features({"energy":0.9,"valence":0.8,"danceability":0.8,"tempo_bpm":125,"loudness_db":-8,"zero_crossing_rate":0.04,"is_major":1.0})
    assert "энергичный" in labels or "танцевальный" in labels
    assert vec["energetic"] == pytest.approx(0.9, 0.01)

def test_resolve_local_file_none():
    assert resolve_local_file(None) is None
    assert resolve_local_file("/nonexistent/path.mp3", "") is None

def test_resolve_local_file_direct(tmp_path):
    f = tmp_path / "test.mp3"
    f.write_bytes(b"fake")
    assert resolve_local_file(str(f)) == f
