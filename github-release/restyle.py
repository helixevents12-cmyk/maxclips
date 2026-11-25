#!/usr/bin/env python3
"""
restyle.py — re-make the clips of a finished (or running) session: new style, different cut.

    python3 restyle.py sessions/eslcs_20261117-232210 --list            # show the clips, numbered
    python3 restyle.py sessions/eslcs_20261117-232210                   # all clips, current style.json
    python3 restyle.py sessions/eslcs_20261117-232210 --only 2 --shift -5    # clip 2, starting 5 s earlier
    python3 restyle.py sessions/eslcs_20261117-232210 --only 1 --length 45   # clip 1, 45 s long

The new versions replace the ones in clips/ (and work/); the credit .txt files stay.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import captions                      # noqa: E402
from cut import cut_vertical         # noqa: E402
from style import load_style         # noqa: E402


def hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def main():
    ap = argparse.ArgumentParser(description="Re-make a session's clips with the current style.json.")
    ap.add_argument("session", help="a folder inside sessions/")
    ap.add_argument("--list", action="store_true", help="just list the clips")
    ap.add_argument("--only", type=int, nargs="+", metavar="N", help="clip number(s) from --list")
    ap.add_argument("--shift", type=float, default=0.0, help="move the cut: -5 = start 5 s earlier, 5 = later")
    ap.add_argument("--length", type=float, help="new clip length in seconds")
    ap.add_argument("--post", type=float, default=10, help="the --post the session used (default 10)")
    ap.add_argument("--mode", choices=["blur", "crop"], help="layout (default: style.json)")
    ap.add_argument("--style", help="path to a style.json (default: the one in this folder)")
    ap.add_argument("--model", default="small", help="Whisper model (default small)")
    ap.add_argument("--language", help="force a caption language, e.g. en, sq")
    ap.add_argument("--no-captions", action="store_true")
    args = ap.parse_args()

    session = Path(args.session)
    table = session / "spikes.csv"
    if not table.exists():
        sys.exit(f"no spikes.csv in {session} — is that a session folder?")
    rows = list(csv.DictReader(open(table, newline="")))
    if not rows:
        sys.exit("this session has no clips yet")

    def name_of(row):
        channel = row["recording"].rsplit("_", 1)[0]
        stamp = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
        return channel, f"{channel}_{stamp:%Y%m%d-%H%M%S}"

    if args.list:
        for i, r in enumerate(rows, 1):
            _, name = name_of(r)
            print(f"{i:>3}  {r['time'][11:]}  {r['messages']:>5} msgs ({float(r['ratio']):.1f}x)  "
                  f"cut {hms(float(r['clip_start']))} +{float(r['clip_seconds']):.0f}s  ->  clips/{name}.mp4")
        return

    style = load_style(args.style)
    use_captions = not args.no_captions
    picked = args.only or list(range(1, len(rows) + 1))
    for n in picked:
        if not 1 <= n <= len(rows):
            print(f"clip {n}: there is no clip with that number (see --list)")
            continue
        r = rows[n - 1]
        channel, name = name_of(r)
        rec = session / "recordings" / r["recording"]
        orig_start, orig_len = float(r["clip_start"]), float(r["clip_seconds"])
        moment_abs = orig_start + max(0.0, orig_len - args.post)     # where chat reacted
        start = max(0.0, orig_start + args.shift)
        length = args.length or orig_len
        final = session / "clips" / f"{name}.mp4"
        print(f"clip {n}: {hms(start)} +{length:.0f}s  ...", flush=True)
        events, note = [], "no captions"
        if use_captions:
            try:
                words = captions.transcribe_words(rec, args.model, args.language, start=start, duration=length)
                events = captions.caption_events(words, style)
                note = f"{len(events)} caption lines" if events else "no speech heard"
            except Exception as e:
                print(f"  captions failed ({e}) — rendering without captions")
        # one encode straight from the recording, captions included
        cut_vertical(rec, start, length, final, credit=channel, mode=args.mode,
                     moment=moment_abs - start, style=style,
                     ass_text=captions.build_ass(events, style) if events else None)
        if events:
            captions.write_srt(events, final.with_suffix(".srt"), style)
        if style.get("save_clean_version"):
            cut_vertical(rec, start, length, session / "work" / f"{name}.mp4", credit=channel,
                         mode=args.mode, moment=moment_abs - start, style=style)
        print(f"  -> {final}  ({note})")


if __name__ == "__main__":
    main()
