"""Spoken replies with Piper TTS and, for languages Gemma hears poorly, Whisper speech recognition
(optional add-ons, downloaded when first needed).

Piper and its voice aren't part of the base install: install() adds them to the app's venv
(~170 MB, skipping onnxruntime's unused sympy) and downloads the chosen language's voice (~63 MB) on first use.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import subprocess
import sys
import threading
import traceback
import urllib.request
import wave
from pathlib import Path
from typing import Callable

_PIPER = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
# Tamil has no official Piper voice. This community one is pinned to a revision and checked by hash.
_TAMIL = "https://huggingface.co/ezhilkumaran/piper-tamil/resolve/5e700a5c256baee2c49198afb25add46ab4b1ae8"
VOICES = {
    "en": {"name": "en_US-lessac-medium", "url": _PIPER, "size": "about 230 MB", "sha256": {}},
    # Gemma's own hearing garbled most Tamil test sentences; Whisper "small" got nearly all of them.
    "ta": {"name": "ta_IN-roja-medium", "url": _TAMIL, "size": "about 1 GB", "whisper": "small", "sha256": {
        ".onnx": "fc40fc629b6c1c6f77203f2c5f2231a9ac12b540da4851cc4a1f49f7e8113de7",
        ".onnx.json": "7722a6391cbaccc3b39eca12c995867ae3ae6447d110d7d74a7c4e3d216f8351"}},
}
# piper-tts + onnxruntime's runtime deps; sympy/mpmath are only used by onnxruntime's model tools
PACKAGES = ["piper-tts", "pathvalidate", "onnxruntime", "numpy", "flatbuffers", "protobuf",
            "coloredlogs", "humanfriendly", "packaging"]


class Voice:
    def __init__(self, voices_dir: Path, emit: Callable[..., None]) -> None:
        self.dir = voices_dir
        self.emit = emit
        self._voices: dict = {}
        self._whisper = None
        self._whisper_lock = threading.Lock()
        self._lock = threading.Lock()
        self.installing = False

    def model_path(self, lang: str) -> Path:
        return self.dir / f"{VOICES[lang]['name']}.onnx"

    def status(self, lang: str = "en") -> dict:
        has_pkg = importlib.util.find_spec("piper") is not None
        path = self.model_path(lang)
        ready = has_pkg and path.is_file() and path.with_suffix(".onnx.json").is_file()
        if VOICES[lang].get("whisper"):
            ready = ready and self.hears(lang)
        return {"ready": ready, "installing": self.installing, "voice": VOICES[lang]["name"], "size": VOICES[lang]["size"]}

    def _whisper_dir(self, lang: str) -> Path:
        return self.dir / "whisper" / VOICES[lang]["whisper"]

    def hears(self, lang: str) -> bool:
        """True when this language has a speech recogniser installed."""
        return (bool(VOICES[lang].get("whisper")) and importlib.util.find_spec("faster_whisper") is not None
                and (self._whisper_dir(lang) / ".ready").is_file())

    def install(self, lang: str = "en") -> None:
        if self.installing:
            return
        self.installing = True
        threading.Thread(target=self._install, args=(lang,), daemon=True, name="voice-install").start()

    def _install(self, lang: str) -> None:
        voice = VOICES[lang]
        try:
            self.emit("voice_setup", step="Installing speech engine…", pct=5)
            subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--disable-pip-version-check",
                            "-q", *PACKAGES], check=True, capture_output=True, text=True)
            if voice.get("whisper"):
                self.emit("voice_setup", step="Installing speech recogniser…", pct=10)
                subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                                "faster-whisper"], check=True, capture_output=True, text=True)
            self.dir.mkdir(parents=True, exist_ok=True)
            for i, ext in enumerate((".onnx.json", ".onnx")):
                dest = self.dir / f"{voice['name']}{ext}"
                if dest.is_file():
                    continue
                part = dest.with_suffix(dest.suffix + ".part")
                req = urllib.request.Request(f"{voice['url']}/{voice['name']}{ext}", headers={"User-Agent": "ek-light-llm/1.0"})
                with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                    total, done, digest = int(r.headers.get("Content-Length") or 0), 0, hashlib.sha256()
                    while block := r.read(1 << 20):
                        f.write(block)
                        digest.update(block)
                        done += len(block)
                        if total and ext == ".onnx":
                            self.emit("voice_setup", step="Downloading voice…", pct=20 + int(78 * done / total))
                expected = voice["sha256"].get(ext)
                if expected and digest.hexdigest() != expected:
                    part.unlink()
                    raise ValueError(f"The downloaded {voice['name']} voice failed its integrity check.")
                part.rename(dest)
            if voice.get("whisper"):
                self.emit("voice_setup", step="Downloading speech recogniser, about 480 MB…", pct=99)
                with self._whisper_lock:
                    self._load_whisper(lang, installing=True)
                (self._whisper_dir(lang) / ".ready").touch()
            self.emit("voice_setup", step="Ready", pct=100, done=True)
            self.warmup(lang)
        except subprocess.CalledProcessError as e:
            self.emit("voice_setup", error=(e.stderr or str(e)).strip()[-400:])
        except Exception as e:  # noqa: BLE001
            self.emit("voice_setup", error=str(e))
        finally:
            self.installing = False

    def _load(self, lang: str):
        if lang not in self._voices:
            from piper import PiperVoice

            self._voices.clear()  # one voice in memory at a time
            self._voices[lang] = PiperVoice.load(str(self.model_path(lang)))
        return self._voices[lang]

    def _load_whisper(self, lang: str, installing: bool = False):
        if self._whisper is None:
            from faster_whisper import WhisperModel

            path = self._whisper_dir(lang)
            path.mkdir(parents=True, exist_ok=True)
            self._whisper = WhisperModel(VOICES[lang]["whisper"], device="cpu", compute_type="int8",
                                         download_root=str(path), local_files_only=not installing)
        return self._whisper

    def transcribe(self, audio_path: str, lang: str) -> str:
        """What was said in the clip, or "" when this language has no recogniser or nothing was understood."""
        if not self.hears(lang):
            return ""
        try:
            with self._whisper_lock:
                segments, _ = self._load_whisper(lang).transcribe(audio_path, language=lang, beam_size=1,
                                                                  condition_on_previous_text=False)
                return "".join(s.text for s in segments).strip()
        except Exception:  # noqa: BLE001  (the caller falls back to Gemma's own hearing)
            traceback.print_exc()
            return ""

    def release(self) -> None:
        """Frees the voices and the recogniser; they reload when next used."""
        with self._whisper_lock:
            self._whisper = None
        with self._lock:
            self._voices.clear()

    def warmup(self, lang: str = "en") -> None:
        """Loads the voice in the background so the first spoken sentence isn't delayed."""
        def run():
            with self._lock:
                self._load(lang)
            if self.hears(lang):
                with self._whisper_lock:
                    self._load_whisper(lang)
        if self.status(lang)["ready"]:
            threading.Thread(target=run, daemon=True, name="voice-warmup").start()

    def speak(self, text: str, lang: str = "en") -> str:
        """Synthesizes text and returns it as a WAV data URL."""
        text = (text or "").strip()
        if not text:
            return ""
        with self._lock:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                self._load(lang).synthesize_wav(text[:1500], w)
        return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()
