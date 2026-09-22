from app.services.vibe import analyze_track, vibe_similarity, detect_mood
from app.services.orchestrator import orchestrate, create_energy_wave

def test_vibe_detect():
    assert detect_mood(analyze_track("metal")) in ("energetic","aggressive")
    assert detect_mood(analyze_track("ambient")) in ("calm","sad")

def test_vibe_similarity():
    a = analyze_track("pop"); b = analyze_track("pop")
    assert vibe_similarity(a,b) == 1.0

def test_orchestrator():
    class T:
        def __init__(self, genre):
            self.genre = genre; self.id="x"
    tracks = [T("metal"), T("ambient"), T("pop")]
    out = orchestrate(tracks)
    assert len(out)==3
    wave = create_energy_wave(tracks, segments=2)
    assert len(wave)==3
