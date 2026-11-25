#!/usr/bin/env python3
"""
captions.py — run Whisper on a clip and burn short, punchy subtitles into it.

    python3 captions.py clip.mp4                       # -> clip.captioned.mp4 (+ .srt)
    python3 captions.py clip.mp4 -o final.mp4 --model small --language en

Uses faster-whisper (the same Whisper models, several times faster on CPU) and falls
back to the openai-whisper package if that's what is installed. The model downloads
once on first use (small ~ 480 MB). Language is auto-detected unless you pass one.

Best quality: let clipper.py / restyle.py / `cut.py --captions` burn the captions in the
same render as the cut (one encode). This script burns into an existing clip (a second encode).

Captions are 1-3 words at a time in heavy white type with a black outline, placed just
under the video; the word being spoken lights up in the accent colour from style.json.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from style import ass_color, load_style

Word = Tuple[float, float, str]
_MODELS: dict = {}


def load_model(name: str = "small"):
    """Load (and cache) a Whisper model. Returns (backend, model)."""
    if name in _MODELS:
        return _MODELS[name]
    try:
        from faster_whisper import WhisperModel
        device, ctype = "cpu", "int8"
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                device, ctype = "cuda", "float16"
        except Exception:
            pass
        _MODELS[name] = ("faster", WhisperModel(name, device=device, compute_type=ctype))
    except ImportError:
        import whisper  # openai-whisper
        _MODELS[name] = ("openai", whisper.load_model(name))
    return _MODELS[name]


def load_audio(path, sr: int = 16000, start: Optional[float] = None, duration: Optional[float] = None):
    """Decode audio with ffmpeg into 16 kHz mono float32 (what Whisper expects), optionally
    only [start, start+duration] of a long recording. Doing it ourselves avoids
    faster-whisper's PyAV decoder, which breaks with newer PyAV."""
    import numpy as np
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start is not None:                      # seek 5 s early, trim exactly after decoding
        fast = max(0.0, start - 5.0)
        cmd += ["-ss", f"{fast:.3f}", "-i", str(path), "-ss", f"{start - fast:.3f}"]
    else:
        cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    r = subprocess.run(cmd + ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0:
        return np.zeros(0, dtype=np.float32)          # no audio track
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def transcribe_words(path, model_name: str = "small", language: Optional[str] = None,
                     start: Optional[float] = None, duration: Optional[float] = None) -> List[Word]:
    """Word timestamps (relative to `start` when given) for a clip or a range of a recording."""
    audio = load_audio(path, start=start, duration=duration)
    if audio.size < 16000:                                # under a second of sound
        return []
    backend, model = load_model(model_name)
    if backend == "faster":
        segments, _info = model.transcribe(
            audio, language=language, word_timestamps=True,
            vad_filter=True, condition_on_previous_text=False)
        return [(w.start, w.end, w.word) for s in segments for w in (s.words or [])]
    result = model.transcribe(audio, language=language, word_timestamps=True,
                              condition_on_previous_text=False)
    return [(w["start"], w["end"], w["word"]) for s in result["segments"] for w in s.get("words", [])]


def chunk_words(words: List[Word], max_words: int = 3, max_chars: int = 18,
                max_gap: float = 0.6, max_dur: float = 1.6) -> List[Tuple[float, float, List[Word]]]:
    """Group words into short caption events: (start, end, words)."""
    chunks: List[List[Word]] = []
    cur: List[Word] = []
    for s, e, t in words:
        t = t.strip()
        if not t:
            continue
        if cur:
            length = sum(len(w[2]) + 1 for w in cur) + len(t)
            if (len(cur) >= max_words or s - cur[-1][1] > max_gap or e - cur[0][0] > max_dur
                    or length > max_chars or cur[-1][2][-1:] in ".!?,"):
                chunks.append(cur)
                cur = []
        cur.append((s, e, t))
    if cur:
        chunks.append(cur)

    events = []
    for i, c in enumerate(chunks):
        start = c[0][0]
        end = max(c[-1][1], start + 0.35) + 0.2              # hold each line a beat
        if i + 1 < len(chunks):
            end = min(end, chunks[i + 1][0][0])              # but never overlap the next
        if end > start:
            events.append((start, end, c))
    return events


def _ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _clean(t: str, upper: bool) -> str:
    t = t.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ").strip()
    return t.upper() if upper else t


def caption_events(words: List[Word], style: Optional[dict] = None):
    """Words -> short caption lines sized so each fits on one row."""
    style = style or load_style()
    size = int(style.get("caption_size", 100))
    return chunk_words(words, max_chars=max(8, int(940 / (0.72 * size))))


def _srt_time(t: float) -> str:
    ms = max(0, round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(events, path, style: Optional[dict] = None) -> Path:
    """Plain .srt next to the clip — for platform captions or editing in DaVinci."""
    style = style or load_style()
    upper = bool(style.get("captions_upper", True))
    blocks = [f"{i}\n{_srt_time(s)} --> {_srt_time(e)}\n{' '.join(_clean(w[2], upper) for w in words)}\n"
              for i, (s, e, words) in enumerate(events, 1)]
    Path(path).write_text("\n".join(blocks), encoding="utf-8")
    return Path(path)


def build_ass(events, style: Optional[dict] = None, margin_v: int = 500) -> str:
    style = style or load_style()
    font = style.get("caption_font_name") or "Arial"
    size = int(style.get("caption_size", 100))
    upper = bool(style.get("captions_upper", True))
    accent = ass_color(style.get("accent", "#4DA8FF"))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,1,0,1,7,3,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    pop = r"{\fscx80\fscy80\t(0,90,\fscx100\fscy100)}"      # quick scale-in pop
    lines = []
    for cs, ce, words in events:
        texts = [_clean(w[2], upper) for w in words]
        if not style.get("caption_highlight", True):
            lines.append(f"Dialogue: 0,{_ass_time(cs)},{_ass_time(ce)},Cap,,0,0,0,,{pop}{' '.join(texts)}")
            continue
        for i, (ws, _we, _t) in enumerate(words):        # one event per spoken word
            s = cs if i == 0 else ws
            e = words[i + 1][0] if i + 1 < len(words) else ce
            if e <= s:
                continue
            text = " ".join(f"{{\\c{accent}}}{t}{{\\c&H00FFFFFF&}}" if j == i else t
                            for j, t in enumerate(texts))
            lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,{pop if i == 0 else ''}{text}")
    return head + "\n".join(lines) + "\n"


def burn(src, ass_text: str, out, style: Optional[dict] = None) -> Path:
    style = style or load_style()
    src, out = Path(src).resolve(), Path(out).resolve()
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "subs.ass").write_text(ass_text, encoding="utf-8")
        for f in {style.get("font"), style.get("font_heavy")} - {None}:
            shutil.copy(f, td)                   # fontsdir=. picks these up
        # run inside the temp dir so the filter sees a plain relative filename (no path escaping)
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-vf", "ass=subs.ass:fontsdir=.", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out)],
            cwd=td, capture_output=True, text=True, start_new_session=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg caption burn failed: {r.stderr.strip()[-800:]}")
    return out


def caption_clip(src, out, model: str = "small", language: Optional[str] = None,
                 style: Optional[dict] = None) -> int:
    """Transcribe src, burn captions into out. Returns the number of caption lines."""
    style = style or load_style()
    events = caption_events(transcribe_words(src, model, language), style)
    if not events:                       # no speech: keep the clip as is
        shutil.copyfile(src, out)
        return 0
    burn(src, build_ass(events, style), out, style)
    write_srt(events, Path(out).with_suffix(".srt"), style)
    return len(events)


def main():
    ap = argparse.ArgumentParser(description="Whisper captions burned into a clip.")
    ap.add_argument("src")
    ap.add_argument("-o", "--out", help="output file (default: <src>.captioned.mp4)")
    ap.add_argument("--model", default="small", help="tiny, base, small, medium, large-v3 (default small)")
    ap.add_argument("--language", help="e.g. en, sq, de — default: auto-detect")
    ap.add_argument("--style", help="path to a style.json (default: the one next to this script)")
    args = ap.parse_args()
    out = args.out or str(Path(args.src).with_suffix("")) + ".captioned.mp4"
    n = caption_clip(args.src, out, args.model, args.language, load_style(args.style))
    print(f"{out}  ({n} caption lines)")


if __name__ == "__main__":
    main()
