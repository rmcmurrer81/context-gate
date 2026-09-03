from context_gate.voice import MAX_SPEECH_CHARS, browser_speaker_html


def test_browser_speaker_is_local_click_control_and_escapes_script_end() -> None:
    markup = browser_speaker_html("Useful answer </script> with details")
    assert "speechSynthesis" in markup
    assert "Speaker on" in markup
    assert "stays on this device" in markup
    assert "Useful answer </script>" not in markup
    assert "Useful answer <\\/script>" in markup


def test_browser_speaker_bounds_spoken_text() -> None:
    markup = browser_speaker_html("x" * (MAX_SPEECH_CHARS + 100))
    assert "x" * MAX_SPEECH_CHARS in markup
    assert "x" * (MAX_SPEECH_CHARS + 1) not in markup
