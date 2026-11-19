#!/usr/bin/env python3
"""EK Video: makes an MP4 from a JSON description, using ffmpeg.

As an MCP plugin it offers create_video. It also runs by hand:
    python video_server.py input.json [-o out.mp4]

{
  "title": "Launch",            file name; optional
  "width": 1280, "height": 720, "fps": 30,      optional
  "background": "#101828",      default scene background
  "transition": "fade",         fade, dissolve, wipeleft, wiperight, slideleft, slideright or none
  "transition_seconds": 0.5,
  "music": "song.mp3",          file or https link, looped quietly under the video
  "music_volume": 0.15,
  "scenes": [
    {
      "title": "Big heading",
      "text": "Smaller text",
      "narration": "Spoken aloud with the offline voice (English or Tamil)",
      "duration": 4,            seconds; by default fits the narration or text
      "background": "#0b5",
      "image": "photo.jpg",     file or https link, fills the frame
      "video": "clip.mp4",      file or https link, fills the frame, its sound is kept unless narrated
      "zoom": true,             slow zoom on an image
      "position": "center",     text position: top, center or bottom
      "color": "#ffffff",
      "font": "Noto Sans"
    }
  ]
}
Paths in a JSON file are relative to that file.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave
from pathlib import Path

from safe_fetch import fetch_public

APP_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = Path.home() / "Videos" / "EK Light"
MAX_SCENES = 100
MAX_TOTAL_SECONDS = 1800
MAX_SIDE = 3840
MAX_DOWNLOAD = 300_000_000
FFMPEG_TIMEOUT = 1500  # seconds for one ffmpeg run; the plugin's whole call may take 30 minutes
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
TYPE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/bmp": ".bmp",
            "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
            "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mp4": ".m4a",
            "audio/ogg": ".ogg", "audio/flac": ".flac"}
TRANSITIONS = {"fade", "dissolve", "wipeleft", "wiperight", "slideleft", "slideright", "none"}
POSITIONS = {"top": 8, "center": 5, "bottom": 2}
COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
FONT_RE = re.compile(r"[\w \-]{1,60}$")
SCENE_KEYS = {"title", "text", "narration", "duration", "background", "image", "video", "zoom", "position", "color", "font"}
SPEC_KEYS = {"title", "width", "height", "fps", "background", "transition", "transition_seconds", "music", "music_volume",
             "scenes", "output"}
AUDIO_FORMAT = ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"]
SAFE_INPUT = ["-protocol_whitelist", "file,pipe,fd"]  # a crafted media file can't make ffmpeg reach the network


class SpecError(ValueError):
    pass


# ---------- checking the JSON ----------
def _number(value, name: str, low: float, high: float, problems: list[str]):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        problems.append(f"{name} must be a number from {low:g} to {high:g} (got {value!r}).")
        return low
    return value


# Names Gemma (and people) often use for scene fields.
SCENE_ALIASES = {"scene": "title", "heading": "title", "headline": "title", "name": "title", "subtitle": "text",
                 "caption": "text", "body": "text", "description": "text", "backgrounds": "background", "bg": "background",
                 "voice": "narration", "voiceover": "narration", "speech": "narration", "seconds": "duration",
                 "photo": "image", "picture": "image", "clip": "video"}


def _validate(spec) -> dict:
    """Returns the spec with defaults filled in, or raises SpecError listing every problem at once."""
    if isinstance(spec, dict) and isinstance(spec.get("scenes"), list):
        spec = dict(spec, scenes=[{SCENE_ALIASES.get(k, k): v for k, v in sc.items()} if isinstance(sc, dict) else sc
                                  for sc in spec["scenes"]])
    if not isinstance(spec, dict):
        raise SpecError('The JSON must be an object with a "scenes" list.')
    problems: list[str] = []
    for k in set(spec) - SPEC_KEYS:
        problems.append(f'Unknown field "{k}". Allowed: {", ".join(sorted(SPEC_KEYS))}.')
    out = {"title": str(spec.get("title") or "video"), "output": str(spec.get("output") or "")}
    for key, default, low, high in (("width", 1280, 16, MAX_SIDE), ("height", 720, 16, MAX_SIDE), ("fps", 30, 1, 60)):
        out[key] = _number(spec.get(key, default), key, low, high, problems)
    out["width"], out["height"], out["fps"] = int(out["width"]) // 2 * 2, int(out["height"]) // 2 * 2, int(out["fps"])
    out["transition"] = spec.get("transition", "fade")
    if out["transition"] not in TRANSITIONS:
        problems.append(f"transition must be one of {', '.join(sorted(TRANSITIONS))}.")
    out["transition_seconds"] = _number(spec.get("transition_seconds", 0.5), "transition_seconds", 0, 5, problems)
    if out["transition"] == "none":
        out["transition_seconds"] = 0
    out["music_volume"] = _number(spec.get("music_volume", 0.15), "music_volume", 0, 1, problems)
    out["music"] = spec.get("music") or ""
    out["background"] = spec.get("background", "#101828")
    if not (isinstance(out["background"], str) and COLOR_RE.match(out["background"])):
        problems.append('background must be a colour like "#101828".')
    scenes = spec.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise SpecError(" ".join(problems + ['"scenes" must be a list with at least one scene.']))
    if len(scenes) > MAX_SCENES:
        problems.append(f"At most {MAX_SCENES} scenes are allowed.")
    out["scenes"] = []
    for i, sc in enumerate(scenes, 1):
        where = f"Scene {i}"
        if not isinstance(sc, dict):
            problems.append(f"{where} must be an object.")
            continue
        for k in set(sc) - SCENE_KEYS:
            problems.append(f'{where}: unknown field "{k}". Allowed: {", ".join(sorted(SCENE_KEYS))}.')
        s = {k: sc.get(k) for k in SCENE_KEYS}
        for k in ("title", "text", "narration"):
            s[k] = "" if s[k] is None else str(s[k])
        if not any((s["title"], s["text"], s["narration"], s["image"], s["video"])):
            problems.append(f"{where} is empty: give it a title, text, narration, image or video.")
        if s["image"] and s["video"]:
            problems.append(f"{where}: use image or video, not both.")
        if s["duration"] is not None:
            s["duration"] = _number(s["duration"], f"{where} duration", 0.5, 600, problems)
        s["background"] = s["background"] or out["background"]
        for k in ("background", "color"):
            if s[k] and not (isinstance(s[k], str) and COLOR_RE.match(s[k])):
                problems.append(f'{where}: {k} must be a colour like "#101828".')
        s["color"] = s["color"] or "#ffffff"
        s["position"] = s["position"] or ("bottom" if s["image"] or s["video"] else "center")
        if s["position"] not in POSITIONS:
            problems.append(f"{where}: position must be top, center or bottom.")
        if s["font"] and not (isinstance(s["font"], str) and FONT_RE.match(s["font"])):
            problems.append(f"{where}: font must be a plain font name.")
        s["zoom"] = bool(s["zoom"])
        out["scenes"].append(s)
    if problems:
        raise SpecError(" ".join(problems))
    return out


# ---------- media ----------
class _Media:
    """Finds media named in the JSON: local files of an allowed type, or public links it downloads."""

    def __init__(self, tmp: Path, base: Path) -> None:
        self.tmp, self.base, self.n = tmp, base, 0

    def get(self, ref, allowed: set[str], what: str) -> Path:
        ref = str(ref)
        if ref.startswith(("http://", "https://")):
            _, ctype, _, raw = fetch_public(ref, MAX_DOWNLOAD, headers={"User-Agent": "ek-light-llm"}, timeout=60)
            ext = TYPE_EXT.get(ctype) or Path(ref.split("?")[0]).suffix.lower()
            if ext not in allowed:
                raise SpecError(f"{ref} is {ctype}, which can't be used as {what}.")
            self.n += 1
            path = self.tmp / f"download{self.n}{ext}"
            path.write_bytes(raw)
            return path
        path = (self.base / os.path.expanduser(ref)).resolve()
        if path.suffix.lower() not in allowed:
            raise SpecError(f"{ref} can't be used as {what}; allowed types: {', '.join(sorted(allowed))}.")
        if not path.is_file():
            raise SpecError(f"File not found: {path}")
        return path


def _run(cmd: list[str], cwd: Path, timeout: float = FFMPEG_TIMEOUT) -> None:
    try:
        r = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg took longer than {timeout / 60:g} minutes and was stopped.") from None
    if r.returncode:
        raise RuntimeError("ffmpeg failed: " + r.stderr.strip()[-600:])


def _check_image(path: Path) -> None:
    """A file that only looks like an image would make ffmpeg's -loop spin forever, so decode it first."""
    try:
        _run(["ffmpeg", "-v", "error", *SAFE_INPUT, "-i", str(path), "-frames:v", "1", "-f", "null", "-"], path.parent, 60)
    except RuntimeError:
        raise SpecError(f"{path.name} isn't a readable image.") from None


def _probe(path: Path) -> tuple[float, bool]:
    """Returns a media file's duration and whether it has sound."""
    r = subprocess.run(["ffprobe", "-v", "error", *SAFE_INPUT, "-show_entries", "format=duration:stream=codec_type",
                        "-of", "json", str(path)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise SpecError(f"{path.name} isn't a readable media file.")
    info = json.loads(r.stdout)
    duration = float(info.get("format", {}).get("duration") or 0)
    return duration, any(s.get("codec_type") == "audio" for s in info.get("streams", []))


# ---------- narration ----------
def _has_tamil(text: str) -> bool:
    return any("\u0b80" <= c <= "\u0bff" for c in text)


def _chunks(text: str, limit: int = 1400) -> list[str]:
    """Splits long narration at sentence ends: the voice reads at most 1500 characters at a time."""
    out, cur = [], ""
    for part in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(part) > limit:
            out.append(part[:limit])
            part = part[limit:]
        if cur and len(cur) + len(part) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur} {part}".strip()
    return out + ([cur] if cur else [])


class _Narrator:
    def __init__(self) -> None:
        sys.path.insert(0, str(APP_DIR))
        from voice import Voice

        models = Path(os.environ.get("EK_MODELS_DIR") or APP_DIR / "models")
        self.voice = Voice(models / "voices", lambda *a, **k: None)

    def say(self, text: str, path: Path) -> float | None:
        """Writes the narration as a WAV and returns its length, or None when that language's voice is missing."""
        lang = "ta" if _has_tamil(text) else "en"
        status_path = self.voice.model_path(lang)
        if not status_path.is_file() or not self.voice.status(lang)["voice"]:
            return None
        try:
            import piper  # noqa: F401
        except ImportError:
            return None
        params, frames = None, []
        for chunk in _chunks(text):
            clip = self.voice.speak(chunk, lang)
            if clip:
                with wave.open(io.BytesIO(base64.b64decode(clip.split(",", 1)[1]))) as w:
                    params = w.getparams()
                    frames.append(w.readframes(w.getnframes()))
        if not frames:
            return None
        with wave.open(str(path), "wb") as w:
            w.setparams(params)
            for f in frames:
                w.writeframes(f)
        return sum(len(f) for f in frames) / (params.framerate * params.sampwidth * params.nchannels)


# ---------- drawing text ----------
def _ass_color(color: str) -> str:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(x * 2 for x in c)
    return f"&H00{c[4:6]}{c[2:4]}{c[0:2]}&".upper()


def _ass_text(text: str) -> str:
    """Text from the JSON is shown literally: braces and backslashes can't act as ASS commands."""
    text = text.replace("\r", "").replace("\\", "\\\u2060").replace("{", "\\{").replace("}", "\\}")
    return text.strip().replace("\n", "\\N")


def _tamil_font() -> str:
    for family in ("Noto Sans Tamil", "Lohit Tamil", "FreeSerif"):
        r = subprocess.run(["fc-list", f"{family}", "family"], stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if r.stdout.strip():
            return family
    return "Noto Sans Tamil"


def _write_ass(path: Path, sc: dict, spec: dict, seconds: float) -> None:
    w, h = spec["width"], spec["height"]
    unit = min(w, h)
    # Long text shrinks so it stays on screen.
    length = len(sc["text"])
    body = unit * (0.05 if length < 180 else 0.04 if length < 400 else 0.03)
    head = unit * (0.1 if len(sc["title"]) < 40 else 0.07)
    combined = sc["title"] + sc["text"]
    font = sc["font"] or (_tamil_font() if _has_tamil(combined) else "DejaVu Sans")
    margin = int(unit * 0.08)
    over_media = bool(sc["image"] or sc["video"])
    outline = max(2, int(unit * 0.004)) if over_media else 0
    parts = []
    if sc["title"]:
        parts.append(f"{{\\fs{head:.0f}\\b1}}{_ass_text(sc['title'])}")
    if sc["text"]:
        parts.append(f"{{\\fs{body:.0f}\\b0}}{_ass_text(sc['text'])}")
    end = time.strftime("%H:%M:%S", time.gmtime(seconds + 1)) + ".00"
    path.write_text(
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\n"
        f"PlayResX: {w}\nPlayResY: {h}\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: S,{font},{body:.0f},{_ass_color(sc['color'])},&H000000FF&,&H00000000&,&H80000000&,0,0,0,0,100,100,0,0,1,"
        f"{outline},0,{POSITIONS[sc['position']]},{margin},{margin},{margin},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,{end},S,,0,0,0,," + "\\N".join(parts) + "\n",
        encoding="utf-8",
    )


# ---------- rendering ----------
def _render_scene(i: int, sc: dict, spec: dict, media: _Media, narrator, tmp: Path, warnings: list[str], is_last: bool) -> tuple[Path, float]:
    w, h, fps = spec["width"], spec["height"], spec["fps"]
    voice_path, voice_len = tmp / f"voice{i}.wav", None
    if sc["narration"]:
        voice_len = narrator.say(sc["narration"], voice_path)
        if voice_len is None:
            lang = "Tamil" if _has_tamil(sc["narration"]) else "English"
            warnings.append(f"Scene {i + 1} has no narration: the {lang} voice isn't installed (start voice mode in that language once to get it).")
    clip, clip_len, clip_sound = None, 0.0, False
    if sc["video"]:
        clip = media.get(sc["video"], VIDEO_EXT, "a video")
        clip_len, clip_sound = _probe(clip)
    image = media.get(sc["image"], IMAGE_EXT, "an image") if sc["image"] else None
    if image:
        _check_image(image)

    shown = sc["duration"]
    if shown is None:
        if voice_len:
            shown = voice_len + 0.6
        elif clip:
            shown = clip_len or 5
        else:  # long enough to read
            shown = max(3.0, len((sc["title"] + " " + sc["text"]).split()) / 2.5)
    elif voice_len and voice_len + 0.2 > shown:
        warnings.append(f"Scene {i + 1}: duration {shown:g}s was extended to {voice_len + 0.6:.1f}s to fit its narration.")
        shown = voice_len + 0.6
    # The fade into the next scene overlaps this one's end, so add that time and nothing gets cut.
    total = shown + (0 if is_last else spec["transition_seconds"])
    fill = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
    cmd = ["ffmpeg", "-y", "-v", "error", *SAFE_INPUT]
    if clip:
        cmd += ["-stream_loop", "-1", "-i", str(clip)]
        chain = f"{fill},fps={fps}"
    elif image:
        if sc["zoom"]:
            frames = max(2, int(total * fps))
            step = 0.12 / frames
            cmd += ["-i", str(image)]
            # Scaling up first keeps the slow zoom from shaking.
            chain = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
                     f"zoompan=z='1+{step:.7f}*on':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={fps},setsar=1")
        else:
            cmd += ["-loop", "1", "-framerate", str(fps), "-i", str(image)]
            chain = fill
    else:
        cmd += ["-f", "lavfi", "-i", f"color=c={_ffmpeg_color(sc['background'])}:s={w}x{h}:r={fps}"]
        chain = "setsar=1"
    if sc["title"] or sc["text"]:
        ass = f"s{i}.ass"
        _write_ass(tmp / ass, sc, spec, total)
        chain += f",ass={ass}"
    if voice_len:
        cmd += ["-i", voice_path.name]
        audio_map = ["-map", "1:a"]
    elif clip and clip_sound:
        audio_map = ["-map", "0:a:0"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        audio_map = ["-map", "1:a"]
    out = tmp / f"scene{i}.mp4"
    cmd += ["-map", "0:v:0", *audio_map, "-vf", chain, "-af", "aresample=44100,apad", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", *AUDIO_FORMAT, str(out)]
    _run(cmd, tmp)
    return out, total


def _ffmpeg_color(color: str) -> str:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(x * 2 for x in c)
    return "0x" + c


def _join(scenes: list[tuple[Path, float]], spec: dict, tmp: Path) -> Path:
    if len(scenes) == 1:
        return scenes[0][0]
    out = tmp / "joined.mp4"
    t = spec["transition_seconds"]
    if not t:
        (tmp / "list.txt").write_text("".join(f"file '{p.name}'\n" for p, _ in scenes))
        _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", str(out)], tmp)
        return out
    inputs, graph = [], []
    vlast, alast, offset = "0:v", "0:a", 0.0
    for i, (p, seconds) in enumerate(scenes):
        inputs += ["-i", p.name]
        if i:
            offset += scenes[i - 1][1] - t
            graph.append(f"[{vlast}][{i}:v]xfade=transition={spec['transition']}:duration={t}:offset={offset:.3f}[v{i}]")
            graph.append(f"[{alast}][{i}:a]acrossfade=d={t}[a{i}]")
            vlast, alast = f"v{i}", f"a{i}"
    (tmp / "graph.txt").write_text(";".join(graph))
    _run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex_script", "graph.txt", "-map", f"[{vlast}]", "-map", f"[{alast}]",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", *AUDIO_FORMAT, str(out)], tmp)
    return out


def _output_path(spec: dict, explicit: Path | None) -> Path:
    if explicit:
        path = explicit
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        # From JSON only a file name counts, so a description can't write outside the videos folder.
        name = Path(spec["output"]).stem if spec["output"] else spec["title"]
        name = unicodedata.normalize("NFC", name)
        # Keeps letters and their vowel signs in any language (Tamil's are combining marks), drops the rest.
        name = "".join(c for c in name if c in " -_" or unicodedata.category(c)[0] in "LMN").strip()[:80] or "video"
        name = re.sub(r"\s+", "-", name)  # Gemma tends to shorten names with spaces when it repeats them
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUTPUT_DIR / f"{name}.mp4"
    n, base = 1, path
    while path.exists():  # never overwrites an earlier video
        n += 1
        path = base.with_name(f"{base.stem}-{n}{base.suffix}")
    return path


def render(raw_spec, base_dir: Path | None = None, output: Path | None = None) -> dict:
    """Makes the video. Raises SpecError for a problem in the JSON, RuntimeError if ffmpeg fails."""
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError("ffmpeg isn't installed. Install it with: sudo apt install ffmpeg")
    spec = _validate(raw_spec)
    started = time.time()
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ek-video-") as t:
        tmp = Path(t)
        media = _Media(tmp, (base_dir or Path.home()).resolve())
        narrator = _Narrator() if any(s["narration"] for s in spec["scenes"]) else None
        last = len(spec["scenes"]) - 1
        scenes = []
        total = 0.0
        for i, sc in enumerate(spec["scenes"]):
            scenes.append(_render_scene(i, sc, spec, media, narrator, tmp, warnings, i == last))
            total += scenes[-1][1]
            if total > MAX_TOTAL_SECONDS:
                raise SpecError(f"The video would be longer than {MAX_TOTAL_SECONDS // 60} minutes.")
        joined = _join(scenes, spec, tmp)
        path = _output_path(spec, output)
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", joined.name]
        if spec["music"]:
            song = media.get(spec["music"], AUDIO_EXT, "music")
            seconds = _probe(joined)[0]
            fade = max(0.0, seconds - 2)
            # amix halves both inputs, so the sum is doubled back.
            cmd = ["ffmpeg", "-y", "-v", "error", *SAFE_INPUT, "-i", joined.name, "-stream_loop", "-1", "-i", str(song), "-filter_complex",
                   f"[1:a]volume={spec['music_volume']},afade=t=out:st={fade:.2f}:d=2[m];"
                   "[0:a][m]amix=inputs=2:duration=first:dropout_transition=0,volume=2[a]",
                   "-map", "0:v", "-map", "[a]", "-c:v", "copy", *AUDIO_FORMAT, "-movflags", "+faststart", "-t", f"{seconds:.3f}", str(path)]
        else:
            cmd += ["-c", "copy", "-movflags", "+faststart", str(path)]
        _run(cmd, tmp)
    return {"path": str(path), "seconds": round(_probe(path)[0], 1), "size_mb": round(path.stat().st_size / 1e6, 1),
            "scenes": len(spec["scenes"]), "width": spec["width"], "height": spec["height"],
            "render_seconds": round(time.time() - started, 1), "warnings": warnings}


def _describe(r: dict) -> str:
    lines = [f"Video saved: {r['path']}",
             f"{r['seconds']} seconds, {r['scenes']} scenes, {r['width']}x{r['height']}, {r['size_mb']} MB, made in {r['render_seconds']} seconds."]
    return "\n".join(lines + [f"Note: {w}" for w in r["warnings"]])


def _spec_from_lists(title, headings, texts, narrations, backgrounds, seconds) -> dict:
    # Gemma sometimes passes one string instead of a list: one scene per line.
    columns = {k: v.splitlines() if isinstance(v, str) else (v or []) for k, v in
               {"title": headings, "text": texts, "narration": narrations, "background": backgrounds}.items()}
    count = max(len(c) for c in columns.values())
    scenes = []
    for i in range(count):
        # Gemma sometimes leaves placeholders like "[Text for Scene 1]", which mustn't reach the video.
        scene = {k: c[i] for k, c in columns.items() if i < len(c) and c[i] and not re.fullmatch(r"\s*\[.*\]\s*", str(c[i]))}
        if scene.get("narration") and not (scene.get("title") or scene.get("text")):
            scene["text"] = scene["narration"]  # otherwise the screen would be blank
        if seconds:
            scene["duration"] = seconds
        scenes.append(scene)
    return {"title": title or "video", "scenes": scenes}


def _main_cli() -> int:
    ap = argparse.ArgumentParser(description="Make an MP4 from a JSON description.")
    ap.add_argument("json_file", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    args = ap.parse_args()
    try:
        spec = json.loads(args.json_file.read_text(encoding="utf-8"))
        print(_describe(render(spec, args.json_file.resolve().parent, args.output.resolve() if args.output else None)))
        return 0
    except (OSError, ValueError, RuntimeError) as e:  # SpecError and JSON errors are ValueErrors
        print(f"Couldn't make the video: {e}", file=sys.stderr)
        return 1


def _main_mcp() -> None:
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("EK Video")

    # Flat lists, not a nested object: Gemma often writes broken syntax for nested tool arguments.
    # JSON the user pastes or attaches is saved to a file by the app and arrives as json_file.
    @mcp.tool()
    def create_video(json_file: str = "", title: str = "", headings: list[str] | str | None = None, texts: list[str] | str | None = None,
                     narrations: list[str] | str | None = None, backgrounds: list[str] | str | None = None, seconds_per_scene: float = 0) -> str:
        """Make ONE MP4 video (call this once for the whole video, never once per scene) and save it in ~/Videos/EK Light. Give json_file, the path of a .json video description; or describe the video with title and lists holding one item per scene, in order: headings, texts (smaller text), narrations (spoken aloud), backgrounds (colours like "#1d4ed8"). Use "" for a scene that doesn't need an item."""
        base = None
        try:
            if json_file.lstrip().startswith(("{", "[")):  # Gemma sometimes passes the JSON itself
                spec = json.loads(json_file)
                if isinstance(spec, list):  # just the scenes
                    spec = {"title": title or "video", "scenes": spec}
            elif json_file:
                path = Path(os.path.expanduser(json_file)).resolve()
                if path.suffix.lower() != ".json":
                    return "json_file must be the path of a .json file."
                spec, base = json.loads(path.read_text(encoding="utf-8")), path.parent
            else:
                spec = _spec_from_lists(title, headings, texts, narrations, backgrounds, seconds_per_scene)
            return _describe(render(spec, base))
        except (OSError, ValueError, RuntimeError) as e:
            return f"Couldn't make the video: {e}"

    mcp.run()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(_main_cli())
    _main_mcp()
