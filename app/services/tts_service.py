"""
TTS Service: text → MP3 narration via edge-tts (Microsoft Azure voices, free).
Falls back to pyttsx3 for offline use.
Duration is estimated from word count (safe fallback if ffprobe unavailable).
"""
import asyncio, logging, os, subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

try:
    import edge_tts as _edge_tts
    _HAS_EDGE_TTS = True
except ImportError:
    _HAS_EDGE_TTS = False

try:
    import pyttsx3 as _pyttsx3
    _HAS_PYTTSX3 = True
except ImportError:
    _HAS_PYTTSX3 = False

_APP_DIR = Path(__file__).parent.parent
_FFPROBE  = str(_APP_DIR / "bin" / "ffprobe") if (_APP_DIR / "bin" / "ffprobe").exists() else "ffprobe"

BUILTIN_VOICES = [
    {"ShortName": "en-US-AriaNeural",    "Gender": "Female", "Locale": "en-US"},
    {"ShortName": "en-US-GuyNeural",     "Gender": "Male",   "Locale": "en-US"},
    {"ShortName": "en-US-JennyNeural",   "Gender": "Female", "Locale": "en-US"},
    {"ShortName": "en-GB-SoniaNeural",   "Gender": "Female", "Locale": "en-GB"},
    {"ShortName": "en-AU-NatashaNeural", "Gender": "Female", "Locale": "en-AU"},
    {"ShortName": "en-IN-NeerjaNeural",  "Gender": "Female", "Locale": "en-IN"},
]


class TTSService:
    def __init__(self, voice: str = "en-US-AriaNeural"):
        self.voice = voice

    async def generate(self, text: str, output_path: str) -> float:
        """Generate MP3 at output_path; return duration in seconds."""
        text = text.strip()
        if not text:
            text = "."

        if _HAS_EDGE_TTS:
            try:
                comm = _edge_tts.Communicate(text, self.voice)
                await comm.save(output_path)
                return self._duration(output_path, text)
            except Exception as e:
                logger.warning("edge-tts failed (%s), trying fallback", e)

        if _HAS_PYTTSX3:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._pyttsx3_generate, text, output_path
            )

        raise RuntimeError(
            "No TTS backend available. Install edge-tts: pip install edge-tts"
        )

    def _pyttsx3_generate(self, text: str, output_path: str) -> float:
        engine = _pyttsx3.init()
        engine.save_to_file(text, output_path)
        engine.runAndWait()
        return self._duration(output_path, text)

    async def list_voices(self) -> List[dict]:
        if not _HAS_EDGE_TTS:
            return BUILTIN_VOICES
        try:
            voices = await _edge_tts.list_voices()
            return [{"ShortName": v["ShortName"], "Gender": v["Gender"],
                     "Locale": v["Locale"]} for v in voices
                    if v.get("Locale", "").startswith("en-")]
        except Exception:
            return BUILTIN_VOICES

    def _duration(self, audio_path: str, text: str = "") -> float:
        """Get duration via ffprobe; fallback to word-count estimate."""
        try:
            result = subprocess.run(
                [_FFPROBE, "-v", "quiet", "-print_format", "json", "-show_streams", audio_path],
                capture_output=True, text=True, timeout=10,
            )
            import json
            data = json.loads(result.stdout)
            for stream in data.get("streams", []):
                if "duration" in stream:
                    return float(stream["duration"])
        except Exception:
            pass
        # Estimate: ~140 words/min
        words = len(text.split())
        return max(2.0, words / 140 * 60)
