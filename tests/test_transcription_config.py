import importlib

import pytest

from live_captions import LiveCaptionRelay
from transcription_config import build_transcription_settings, interview_language


@pytest.mark.parametrize("language, code", [("en", "en"), ("fr", "fr"),
                                         ("English", "en"), ("French", "fr")])
def test_selected_language_matches_in_both_transcribers(language, code, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unused-test-value")
    monkeypatch.setenv("TALENTSIFT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TALENTSIFT_CAPTION_DELAY", raising=False)
    app = importlib.import_module("app")
    context = {"candidate_name": "David Afolabi", "job_title": "Python Developer"}
    config = app.build_session_config(language, **context)
    final = config["session"]["audio"]["input"]["transcription"]
    live = LiveCaptionRelay(None, "unused", language, **context).transcription
    assert final["languages"] == live["languages"] == [code]
    assert "language" not in final and "language" not in live
    assert final["prompt"] == live["prompt"]
    assert final["keywords"] == live["keywords"] == ["David Afolabi", "Python Developer"]
    assert live["delay"] == "low"
    assert "delay" not in final
    assert "Conduct the ENTIRE interview in " + app.LANGUAGE_NAMES[code] in config["session"]["instructions"]
    assert config["session"]["audio"]["input"]["turn_detection"]["create_response"] is False


def test_unrecognized_language_is_rejected_instead_of_silently_becoming_english():
    with pytest.raises(ValueError):
        interview_language("invalid")


def test_keyword_context_is_literal_single_line_and_optional():
    settings = build_transcription_settings("fr", "gpt-transcribe",
        candidate_name="  David\n<Afolabi> ", job_title="Python\r\nDeveloper")
    assert settings["keywords"] == ["David Afolabi", "Python Developer"]
    assert "keywords" not in build_transcription_settings("en", "gpt-transcribe")
