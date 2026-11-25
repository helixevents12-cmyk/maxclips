#!/usr/bin/env python3
"""
cut.py — cut a styled vertical 9:16 clip (1080x1920 mp4) out of a recording,
even one that is still being written.

    python3 cut.py recording.ts --start 754 --credit somechannel -o clip.mp4
    python3 cut.py recording.ts --start 754 --credit somechannel --moment 20 -o clip.mp4

The look comes from style.json:
  - full gameplay frame on a blurred, navy-tinted copy of itself, framed by accent lines
    (or "crop": centre crop that fills the screen)
  - "twitch.tv/<channel>" credit at the top
  - hook text: "WAIT FOR IT..." until the moment, then "CHAT WENT CRAZY" in the accent colour
    (needs --moment = seconds into the clip where the moment is; clipper.py passes it)
  - optional watermark, progress bar along the bottom, fade in/out
  - --captions: Whisper subtitles rendered in the SAME pass (one encode from the source)

Crystal render: one encode straight from the recording, Lanczos scaling, x264 "slow"
CRF 17 capped at 20 Mbps (TikTok/Reels/Shorts friendly), BT.709 colour tags, the
source frame rate kept (60 stays 60), audio normalised to -14 LUFS / -1 dBTP.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from style import ff_color, load_style

W, H = 1080, 1920

FALLBACK_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",       # macOS
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",     # Linux
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def media_duration(path) -> Optional[float]:
    """Seconds of media in a file (works on a .ts that is still growing)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def probe(path):
    """(width, height, has_audio) of a media file."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                        "-of", "json", str(path)], capture_output=True, text=True)
    streams = json.loads(r.stdout or "{}").get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("width")), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return int(video.get("width", 1920)), int(video.get("height", 1080)), has_audio


def source_info(path) -> dict:
    """Resolution, fps, codec and overall bitrate of a recording."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height,avg_frame_rate:format=bit_rate",
                        "-of", "json", str(path)], capture_output=True, text=True)
    data = json.loads(r.stdout or "{}")
    v = (data.get("streams") or [{}])[0]
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den) if den and float(den) else 0.0
    br = (data.get("format") or {}).get("bit_rate")
    return {"width": v.get("width"), "height": v.get("height"), "fps": round(fps, 2),
            "codec": v.get("codec_name"), "mbps": round(int(br) / 1e6, 1) if br and br.isdigit() else None}


def quality_notes(src_w: int, src_h: int, mode: str) -> list:
    """Warnings when the 9:16 layout has to enlarge the source."""
    notes = []
    if src_h < 1080:
        notes.append(f"source is only {src_w}x{src_h} — record with --quality best for sharper clips")
    if mode == "crop":
        scale = W / (src_h * 9 / 16)
        if scale > 1.5:
            notes.append(f"crop layout enlarges the picture {scale:.1f}x — blur layout keeps it sharper")
    elif src_w < W:
        notes.append(f"gameplay is enlarged {W / src_w:.1f}x to fill the width")
    return notes


# ---------------------------------------------------------------- filter helpers
def _font(path: Optional[str]) -> str:
    candidates = ([path] if path else []) + FALLBACK_FONTS
    for p in candidates:
        if p and os.path.exists(p):
            return "fontfile='" + p.replace("\\", "/").replace(":", "\\:") + "'"
    return "font='Sans'"


def _txt(s: str) -> str:
    """Text safe to put inside drawtext's text='...'."""
    s = s.replace("\\", "").replace("'", "\u2019").replace("%", "\uFF05")
    return s.replace(":", "\\:")


def _fit(text: str, size: int, width: int = 980) -> int:
    """Shrink the font size so a heavy uppercase line fits the frame width."""
    return max(36, min(size, int(width / (0.72 * max(1, len(text))))))


def build_filter(src_w: int, src_h: int, offset: float, dur: float, style: dict,
                 credit: Optional[str], moment: Optional[float], has_audio: bool, mode: str,
                 logo_size: Optional[tuple] = None, subs: bool = False) -> str:
    """logo_size = (w, h) of the logo PNG passed as ffmpeg input 1, or None for no logo.
    subs = burn subs.ass (from the working directory) in the same pass."""
    acc, tint = ff_color(style["accent"]), ff_color(style["tint"])
    font, heavy = _font(style.get("font")), _font(style.get("font_heavy") or style.get("font"))
    trim = f"trim=start={offset:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS"
    chains, main = [], []

    if mode == "blur":
        fg_h = int(round(W * src_h / src_w / 2)) * 2
        top = (H - fg_h) // 2 // 2 * 2
        chains.append(f"[0:v]{trim},split=2[a][b]")
        chains.append(
            "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,"
            f"scale={W}:{H},eq=brightness=-0.10,drawbox=x=0:y=0:w=iw:h=ih:color={tint}@0.45:t=fill[bg]")
        chains.append(f"[b]scale={W}:{fg_h}:flags=lanczos,setsar=1[fg]")
        main += [f"[bg][fg]overlay=0:{top}", "setsar=1",
                 f"drawbox=x=0:y={top - 6}:w={W}:h=6:color={acc}:t=fill",
                 f"drawbox=x=0:y={top + fg_h}:w={W}:h=6:color={acc}:t=fill"]
        credit_y = 230                      # below TikTok/Reels top tabs
        hook_y = top - 190
    elif mode == "crop":
        main += [f"[0:v]{trim}", f"scale=-2:{H}:flags=lanczos", f"crop={W}:{H}", "setsar=1"]
        credit_y, hook_y = 230, 470
    else:
        raise ValueError("layout must be 'blur' or 'crop'")

    if logo_size:                           # brand logo on top, credit moves under it
        lw = int(style.get("logo_width", 560))
        lh = int(round(lw * logo_size[1] / logo_size[0]))
        ly = int(style.get("logo_y", 205))
        chains.append(f"[1:v]scale={lw}:{lh},format=rgba[lg]")
        chains.append(",".join(main) + "[base]")
        main = [f"[base][lg]overlay=(W-w)/2:{ly}"]
        credit_y = ly + lh + 22
    hook_y = max(credit_y + 120, hook_y)

    if credit:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", credit):
            raise ValueError(f"not a Twitch channel name: {credit!r}")
        main.append(f"drawtext={font}:text='twitch.tv/{credit.lower()}':fontsize=40:fontcolor=white"
                    f":box=1:boxcolor=black@0.55:boxborderw=16:x=(w-text_w)/2:y={credit_y}")

    before, after = style.get("hook_before") or "", style.get("hook_after") or ""
    if moment is not None and (before or after):
        switch = max(1.5, min(dur - 1.0, moment - 3.0))
        shadow = ":shadowx=4:shadowy=4:shadowcolor=black@0.6"
        if before:
            main.append(f"drawtext={heavy}:text='{_txt(before)}':fontsize={_fit(before, 78)}:fontcolor=white"
                        f"{shadow}:x=(w-text_w)/2:y={hook_y}:enable='lt(t,{switch:.2f})'")
        if after:
            main.append(f"drawtext={heavy}:text='{_txt(after)}':fontsize={_fit(after, 78)}:fontcolor={acc}"
                        f"{shadow}:x=(w-text_w)/2:y={hook_y}:enable='gte(t,{switch:.2f})'"
                        f":alpha='min(1,(t-{switch:.2f})/0.25)'")

    if style.get("watermark"):
        wm = style["watermark"]
        main.append(f"drawtext={font}:text='{_txt(wm)}':fontsize=34:fontcolor=white@0.75"
                    f":x=(w-text_w)/2:y=h-95")

    if subs:
        main.append("ass=subs.ass:fontsdir=.")

    tail = []
    if style.get("fade", True) and dur > 2:
        tail = ["fade=t=in:st=0:d=0.3", f"fade=t=out:st={dur - 0.45:.2f}:d=0.45"]

    if style.get("progress_bar", True):
        chains.append(",".join(main) + "[pre]")
        chains.append(f"color=c={acc}:s={W}x10:r=30:d={dur:.3f}[bar]")
        chains.append(",".join([f"[pre][bar]overlay=x='-w+w*t/{dur:.3f}':y=H-h:eof_action=pass"] + tail) + "[v]")
    else:
        chains.append(",".join(main + tail) + "[v]")

    if has_audio:
        afade = ""
        if style.get("fade", True) and dur > 2:
            afade = f",afade=t=in:d=0.3,afade=t=out:st={dur - 0.45:.2f}:d=0.45"
        lufs = float(style.get("loudness_lufs", -14))
        chains.append(f"[0:a]atrim=start={offset:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,"
                      f"loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000{afade}[a]")
    return ";".join(chains)


def cut_vertical(src, start: float, duration: float, out, credit: Optional[str] = None,
                 mode: Optional[str] = None, moment: Optional[float] = None,
                 style: Optional[dict] = None, ass_text: Optional[str] = None) -> Path:
    """Render [start, start+duration] of src into a styled 1080x1920 clip in ONE encode.
    moment   = seconds into the clip where the hype moment is (drives the hook text)
    ass_text = ASS subtitles to burn in the same pass (from captions.py), or None."""
    style = style or load_style()
    mode = mode or style.get("layout", "blur")
    src, out = Path(src).resolve(), Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    src_w, src_h, has_audio = probe(src)

    # Seeking inside MPEG-TS can land a keyframe late, so seek 5 s early and
    # trim to the exact frame after decoding.
    start = max(0.0, start)
    fast = max(0.0, start - 5.0)
    logo = style.get("logo")
    logo_size = probe(logo)[:2] if logo else None
    graph = build_filter(src_w, src_h, start - fast, duration, style, credit, moment, has_audio, mode,
                         logo_size, subs=bool(ass_text))

    crf = str(style.get("crf", 17))
    preset = str(style.get("preset", "slow"))
    maxrate = str(style.get("max_bitrate", "20M"))
    digits = maxrate.rstrip("Mm")
    bufsize = f"{int(digits) * 2}M" if digits.isdigit() else maxrate
    part = out.with_name(out.stem + ".part" + out.suffix)          # finished file appears atomically
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{fast:.3f}", "-i", str(src)]
    if logo:
        cmd += ["-i", str(Path(logo).resolve())]
    cmd += ["-filter_complex", graph, "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", preset, "-crf", crf, "-maxrate", maxrate, "-bufsize", bufsize,
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-movflags", "+faststart", str(part)]
    with tempfile.TemporaryDirectory() as td:
        if ass_text:
            (Path(td) / "subs.ass").write_text(ass_text, encoding="utf-8")
            for f in {style.get("font"), style.get("font_heavy")} - {None}:
                shutil.copy(f, td)                  # fontsdir=. picks these up
        # own session: a Ctrl+C in clipper.py lets an in-progress clip finish
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=td, start_new_session=True)
    if r.returncode != 0 or not part.exists():
        part.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg render failed: {r.stderr.strip()[-800:]}")
    os.replace(part, out)
    return out


def main():
    ap = argparse.ArgumentParser(description="Cut a styled vertical 9:16 clip from a recording.")
    ap.add_argument("src", help="recording (.ts/.mp4/.mkv)")
    ap.add_argument("--start", type=float, required=True, help="clip start, seconds into the recording")
    ap.add_argument("--duration", type=float, default=30.0, help="clip length in seconds (default 30)")
    ap.add_argument("--credit", metavar="CHANNEL", help="burn in 'twitch.tv/CHANNEL'")
    ap.add_argument("--moment", type=float, help="seconds into the clip where the moment is (shows the hook)")
    ap.add_argument("--mode", choices=["blur", "crop"], help="layout (default: from style.json)")
    ap.add_argument("--style", help="path to a style.json (default: the one next to this script)")
    ap.add_argument("--captions", action="store_true", help="Whisper captions in the same pass")
    ap.add_argument("--model", default="small", help="Whisper model (default small)")
    ap.add_argument("--language", help="caption language, e.g. en, sq (default auto)")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()
    if not args.credit:
        print("note: no --credit given; clips you post should credit the streamer", file=sys.stderr)
    style = load_style(args.style)
    mode = args.mode or style.get("layout", "blur")
    w, h, _ = probe(args.src)
    for note in quality_notes(w, h, mode):
        print("quality:", note, file=sys.stderr)
    events = []
    ass_text = None
    if args.captions:
        import captions
        try:
            words = captions.transcribe_words(args.src, args.model, args.language,
                                              start=args.start, duration=args.duration)
            events = captions.caption_events(words, style)
            ass_text = captions.build_ass(events, style) if events else None
        except Exception as e:
            print(f"captions failed ({e}) — rendering without captions", file=sys.stderr)
    out = cut_vertical(args.src, args.start, args.duration, args.out, args.credit, mode,
                       args.moment, style, ass_text)
    if events:
        captions.write_srt(events, Path(out).with_suffix(".srt"), style)
    print(out, f"({len(events)} caption lines)" if args.captions else "")


if __name__ == "__main__":
    main()
