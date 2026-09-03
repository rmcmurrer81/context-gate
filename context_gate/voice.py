"""Private, dependency-free browser speech controls for the local application."""

from __future__ import annotations

import json
import re

MAX_SPEECH_CHARS = 2200


def browser_speaker_html(text: str) -> str:
    """Return click-to-speak controls using only the judge device's Web Speech API."""

    cleaned = re.sub(r"\s+", " ", text).strip()[:MAX_SPEECH_CHARS]
    # Escaping the closing tag keeps even adversarial text inside the JS string.
    encoded = json.dumps(cleaned, ensure_ascii=False).replace("</", "<\\/")
    return f"""
    <div style="font-family:system-ui;color:#cbd5e1;display:flex;align-items:center;
                gap:.55rem;flex-wrap:wrap;padding:.25rem 0;">
      <label style="display:flex;align-items:center;gap:.35rem;cursor:pointer;">
        <input id="cg-speaker" type="checkbox" />
        <strong>Speaker on</strong>
      </label>
      <button id="cg-replay" type="button" style="border:1px solid #475569;
              border-radius:8px;background:#0f172a;color:#e2e8f0;padding:.3rem .65rem;">
        Replay
      </button>
      <button id="cg-stop" type="button" style="border:1px solid #475569;
              border-radius:8px;background:#0f172a;color:#e2e8f0;padding:.3rem .65rem;">
        Stop
      </button>
      <span id="cg-voice-status" style="font-size:.75rem;color:#94a3b8;">
        Friendly English device voice · stays on this device
      </span>
    </div>
    <script>
      const cgText = {encoded};
      const cgSynth = window.speechSynthesis;
      const cgToggle = document.getElementById("cg-speaker");
      const cgStatus = document.getElementById("cg-voice-status");

      function cgPreferredVoice() {{
        const voices = cgSynth ? cgSynth.getVoices() : [];
        const english = voices.filter(v => /^en([-_]|$)/i.test(v.lang || ""));
        return english.find(v => /aria|jenny|samantha|zira|victoria|susan|hazel|serena|ava/i.test(v.name))
          || english[0] || voices[0] || null;
      }}

      function cgSpeak() {{
        if (!cgSynth || !cgText) {{
          cgStatus.textContent = "Speech is unavailable in this browser";
          return;
        }}
        cgSynth.cancel();
        const utterance = new SpeechSynthesisUtterance(cgText);
        const voice = cgPreferredVoice();
        if (voice) utterance.voice = voice;
        utterance.rate = 0.98;
        utterance.pitch = 1.03;
        utterance.onstart = () => {{
          cgStatus.textContent = voice ? `Speaking with ${{voice.name}}` : "Speaking with system voice";
        }};
        utterance.onend = () => {{ cgStatus.textContent = "Finished · text remains visible"; }};
        utterance.onerror = () => {{ cgStatus.textContent = "Speech stopped or unavailable"; }};
        cgSynth.speak(utterance);
      }}

      cgToggle.addEventListener("change", () => {{
        if (cgToggle.checked) cgSpeak();
        else if (cgSynth) cgSynth.cancel();
      }});
      document.getElementById("cg-replay").addEventListener("click", cgSpeak);
      document.getElementById("cg-stop").addEventListener("click", () => {{
        if (cgSynth) cgSynth.cancel();
        cgToggle.checked = false;
        cgStatus.textContent = "Stopped · voice is optional";
      }});
    </script>
    """
