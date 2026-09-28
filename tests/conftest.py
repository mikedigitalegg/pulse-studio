import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _isolated_ai_settings(monkeypatch, tmp_path):
    """Keep the user's real AI provider choice (ai_settings.json, PULSE_AI_*) out of tests."""
    import ableton_web_poc_server as srv

    monkeypatch.setattr(srv, "AI_SETTINGS_FILE", str(tmp_path / "ai_settings.json"))
    monkeypatch.setattr(srv, "ENV_FILE", str(tmp_path / ".env"))
    for var in ("PULSE_AI_PROVIDER", "PULSE_AI_MODEL", "LOCAL_AI_BASE_URL", "LOCAL_AI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
