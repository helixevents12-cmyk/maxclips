#!/usr/bin/env python3
"""
clipper.py — one command: wait for a Twitch channel to go live, record it, watch chat,
and turn every chat spike into a captioned vertical clip.

    python3 clipper.py <channel>

Pipeline
  record.sh        waits for go-live, records from the first second       recordings/*.ts
  chat_counter.py  anonymous IRC, messages per 10 s window                chat_counts.csv
  spikes.py        window > 3x the rolling average -> spike               spikes.csv
  cut.py           30 s around the spike -> 1080x1920 mp4 + streamer credit
  captions.py      Whisper -> burned-in subtitles                         clips/*.mp4

Only channels listed in allowed_channels.txt are clipped. Every clip carries
"twitch.tv/<channel>" on screen plus a ready-to-paste credit caption (.txt).
Ctrl+C stops recording and still finishes the clips already detected.
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import captions                                           # noqa: E402
from chat_counter import CountsCsv, make_counter          # noqa: E402
from cut import cut_vertical, media_duration, quality_notes, source_info   # noqa: E402
from spikes import Spike, SpikeDetector                   # noqa: E402
from style import load_style                               # noqa: E402

log = logging.getLogger("clipper")


@dataclass
class Job:
    spike: Spike
    recording: Path
    start: float          # seconds into the recording
    duration: float
    moment: float         # seconds into the clip where chat reacted
    next_check: float = 0.0


# ---------------------------------------------------------------- setup
def read_allowlist(path: Path) -> set:
    names = set()
    if path.exists():
        for line in path.read_text().splitlines():
            name = line.split("#", 1)[0].strip().lower().lstrip("#")
            if name:
                names.add(name)
    return names


def preflight(captions_on: bool):
    problems = []
    for tool in ("streamlink", "ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            problems.append(f"{tool} not found on PATH")
    if shutil.which("ffmpeg"):
        filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                                 capture_output=True, text=True).stdout
        for f, lib in [("drawtext", "libfreetype")] + ([("ass", "libass")] if captions_on else []):
            if not re.search(rf"^\s*\S+\s+{f}\s", filters, re.M):
                problems.append(f"your ffmpeg has no '{f}' filter (it needs to be built with {lib})")
    if captions_on:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            try:
                import whisper  # noqa: F401
            except ImportError:
                problems.append("Whisper not installed: pip install faster-whisper (or run with --no-captions)")
    if problems:
        sys.exit("can't start:\n  - " + "\n  - ".join(problems))


def setup_logging(logfile: Path):
    fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(logfile)):
        h.setFormatter(fmt)
        log.addHandler(h)


def _pipe_to_log(stream):
    for line in stream:
        line = line.rstrip()
        if line:
            log.info(line if line.startswith("[record]") else f"[record] {line}")


# ---------------------------------------------------------------- recording helpers
def current_recording(rec_dir: Path) -> Optional[Path]:
    files = [p for p in rec_dir.glob("*.ts") if p.stat().st_size > 0]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def is_growing(path: Path, within: float = 20.0) -> bool:
    try:
        return time.time() - path.stat().st_mtime < within
    except FileNotFoundError:
        return False


def hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def plan_clip(spike: Spike, rec_dir: Path, args) -> Optional[Job]:
    """Turn a spike (wall-clock time) into a cut range inside the recording."""
    desc = f"{spike.count} msgs/10s vs avg {spike.baseline:.1f} ({spike.ratio:.1f}x)"
    rec = current_recording(rec_dir)
    if rec is None or not is_growing(rec):
        log.info("[spike] %s — stream isn't being recorded right now, skipped", desc)
        return None
    t0 = time.time()
    dur = media_duration(rec)
    now = (t0 + time.time()) / 2
    if dur is None:
        log.info("[spike] %s — couldn't read the recording yet, skipped", desc)
        return None
    # Measure back from the live end of the file: robust to start-up backlog and to
    # ad-break gaps earlier in the stream. chat_delay = how far chat lags the action.
    moment = dur - (now - spike.start) - args.chat_delay
    start, end = max(0.0, moment - args.pre), moment + args.post
    if end <= 1:
        log.info("[spike] %s — happened before the recording started, skipped", desc)
        return None
    log.info("[spike] %s -> clip %s–%s of %s", desc, hms(start), hms(end), rec.name)
    return Job(spike, rec, start, end - start, moment - start)


def dispatch_ready(pending: List[Job], jobs: queue.Queue, rec_dir: Path, force: bool = False):
    """Hand a job to the clip worker once the recording has its whole range on disk."""
    now = time.time()
    for job in list(pending):
        if not force and now < job.next_check:
            continue
        job.next_check = now + 2
        live = is_growing(job.recording) and current_recording(rec_dir) == job.recording
        if force or not live or (media_duration(job.recording) or 0) >= job.start + job.duration + 1:
            pending.remove(job)
            jobs.put(job)


# ---------------------------------------------------------------- clip worker
def write_credit(path: Path, channel: str, spike: Spike, brand: str = ""):
    when = datetime.fromtimestamp(spike.start).strftime("%d %b %Y, %H:%M")
    by = f"Clipped by {brand} · " if brand else ""
    path.write_text(
        f"Clip from twitch.tv/{channel} — all credit to {channel}.\n"
        f"Watch them live: https://twitch.tv/{channel}\n"
        f"\n"
        f"({by}stream moment {when} · chat hit {spike.ratio:.1f}x its normal pace)\n",
        encoding="utf-8")


def clip_worker(jobs: queue.Queue, channel: str, args, style: dict, clips_dir: Path, work_dir: Path,
                stats: dict):
    captions_on = not args.no_captions
    if captions_on:
        try:
            log.info("[captions] loading Whisper '%s' (downloads once, on the first run)", args.model)
            captions.load_model(args.model)
            log.info("[captions] Whisper ready")
        except Exception as e:
            log.warning("[captions] couldn't load Whisper (%s) — clips will be saved without captions", e)
            captions_on = False

    while True:
        job = jobs.get()
        if job is None:
            return
        t = time.time()
        name = f"{channel}_{datetime.fromtimestamp(job.spike.start):%Y%m%d-%H%M%S}"
        final = clips_dir / f"{name}.mp4"
        try:
            # 1) Whisper on the exact range of the recording  2) ONE render with the captions in it
            events, note = [], "no captions"
            if captions_on:
                try:
                    words = captions.transcribe_words(job.recording, args.model, args.language,
                                                      start=job.start, duration=job.duration)
                    events = captions.caption_events(words, style)
                    note = f"{len(events)} caption lines" if events else "no speech heard"
                except Exception as e:
                    log.warning("[captions] failed on %s (%s) — rendering without captions", name, e)
                    note = "captions failed"
            ass_text = captions.build_ass(events, style) if events else None
            cut_vertical(job.recording, job.start, job.duration, final, credit=channel,
                         mode=args.mode, moment=job.moment, style=style, ass_text=ass_text)
            if events:
                captions.write_srt(events, final.with_suffix(".srt"), style)
            if style.get("save_clean_version"):        # same clip without captions, for DaVinci
                cut_vertical(job.recording, job.start, job.duration, work_dir / f"{name}.mp4",
                             credit=channel, mode=args.mode, moment=job.moment, style=style)
            write_credit(final.with_suffix(".txt"), channel, job.spike, style.get("brand_name", ""))
            stats["clips"] += 1
            log.info("[clip] %s  (%s, %.0fs)", final, note, time.time() - t)
        except Exception as e:
            log.error("[clip] %s failed: %s", name, e)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="Live Twitch clipper: record from go-live, watch chat, clip the spikes.")
    ap.add_argument("channel", help="Twitch channel name (must be in allowed_channels.txt)")
    d = ap.add_argument_group("spike detection")
    d.add_argument("--window", type=int, default=10, help="chat window in seconds (default 10)")
    d.add_argument("--factor", type=float, default=3.0, help="spike = FACTOR x rolling average (default 3)")
    d.add_argument("--history", type=int, default=30, help="windows in the rolling average (default 30 = 5 min)")
    d.add_argument("--warmup", type=int, default=6, help="windows of history before flagging (default 6)")
    d.add_argument("--min-count", type=int, default=5, help="minimum messages for a spike (default 5)")
    c = ap.add_argument_group("clips")
    c.add_argument("--pre", type=float, default=20, help="seconds before the spike (default 20)")
    c.add_argument("--post", type=float, default=10, help="seconds after the spike (default 10)")
    c.add_argument("--chat-delay", type=float, default=0,
                   help="shift clips this many seconds earlier if they tend to start too late "
                        "(default 0: viewers' typing delay roughly cancels the recorder's extra latency)")
    c.add_argument("--mode", choices=["blur", "crop"], help="9:16 layout (default: style.json)")
    c.add_argument("--style", help="path to a style.json (default: the one in this folder)")
    w = ap.add_argument_group("captions")
    w.add_argument("--model", default="small", help="Whisper model: tiny/base/small/medium/large-v3 (default small)")
    w.add_argument("--language", help="force a language, e.g. en, sq, de (default auto-detect)")
    w.add_argument("--upper", action="store_true", help="UPPERCASE captions (style.json default: on)")
    w.add_argument("--no-captions", action="store_true", help="skip Whisper")
    r = ap.add_argument_group("recording")
    r.add_argument("--quality", default="best", help="streamlink quality (default best)")
    r.add_argument("--poll", type=int, default=5, help="seconds between live checks while offline (default 5)")
    default_out = HERE.parent / "sessions" if (HERE.parent / "sessions").is_dir() else HERE / "sessions"
    r.add_argument("--out", default=str(default_out), help="output folder (default: MAXCLIPS/sessions)")
    r.add_argument("--allowlist", default=str(HERE / "allowed_channels.txt"))
    args = ap.parse_args()

    channel = args.channel.lower().lstrip("#")
    if not re.fullmatch(r"[a-z0-9_]{1,25}", channel):
        sys.exit(f"'{args.channel}' isn't a valid Twitch channel name")
    if channel not in read_allowlist(Path(args.allowlist)):
        sys.exit(f"'{channel}' is not in {args.allowlist}.\n"
                 "Add it once the streamer has given you permission (or their clip policy "
                 "allows reuse), then run this again.")
    preflight(not args.no_captions)
    style = load_style(args.style)
    if args.upper:
        style["captions_upper"] = True

    session = Path(args.out) / f"{channel}_{datetime.now():%Y%m%d-%H%M%S}"
    rec_dir, clips_dir, work_dir = session / "recordings", session / "clips", session / "work"
    for p in (rec_dir, clips_dir, work_dir):
        p.mkdir(parents=True, exist_ok=True)
    setup_logging(session / "clipper.log")
    log.info("twitch.tv/%s -> %s", channel, session)
    log.info("spike = >%.0fx the %ds-window average · clips %.0fs before to %.0fs after · credit: twitch.tv/%s",
             args.factor, args.window, args.pre, args.post, channel)

    # 1) recorder
    env = dict(os.environ, QUALITY=args.quality, POLL=str(args.poll))
    rec = subprocess.Popen(["bash", str(HERE / "record.sh"), channel, str(rec_dir)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           env=env, bufsize=1)
    threading.Thread(target=_pipe_to_log, args=(rec.stdout,), daemon=True).start()

    # 2) chat counter
    windows: queue.Queue = queue.Queue()
    counter = make_counter(channel, args.window, log=lambda m: log.info("[chat] %s", m))
    threading.Thread(target=counter.run, args=(windows.put,), daemon=True).start()

    # 4+5) cutter + captioner (one at a time, in the background)
    jobs: queue.Queue = queue.Queue()
    stats = {"spikes": 0, "clips": 0}
    worker = threading.Thread(target=clip_worker,
                              args=(jobs, channel, args, style, clips_dir, work_dir, stats), daemon=True)
    worker.start()

    # 3) spike detection loop
    detector = SpikeDetector(args.factor, args.history, args.warmup, args.min_count,
                             cooldown=args.pre + args.post)
    counts = CountsCsv(session / "chat_counts.csv")
    spikes_csv = open(session / "spikes.csv", "a", buffering=1)
    spikes_csv.write("time,messages,average,ratio,recording,clip_start,clip_seconds\n")
    pending: List[Job] = []
    last_status, minute_msgs = time.time(), 0
    silent_minutes, warned_silent = 0, False
    source_logged = None

    try:
        while rec.poll() is None:
            try:
                win = windows.get(timeout=1)
            except queue.Empty:
                win = None
            if win is not None:
                counts.write(win)
                minute_msgs += win.count
                spike = detector.update(win.start, win.end, win.count, win.complete)
                if spike:
                    stats["spikes"] += 1
                    job = plan_clip(spike, rec_dir, args)
                    if job:
                        pending.append(job)
                        spikes_csv.write(f"{datetime.fromtimestamp(spike.start):%Y-%m-%d %H:%M:%S},"
                                         f"{spike.count},{spike.baseline:.2f},{spike.ratio:.2f},"
                                         f"{job.recording.name},{job.start:.1f},{job.duration:.1f}\n")
            dispatch_ready(pending, jobs, rec_dir)

            cur = current_recording(rec_dir)
            if cur and cur != source_logged and is_growing(cur) and (media_duration(cur) or 0) > 15:
                source_logged = cur
                info = source_info(cur)
                log.info("[record] source %sx%s %sfps %s ~%s Mbps", info["width"], info["height"],
                         info["fps"], info["codec"], info["mbps"])
                for note in quality_notes(info["width"] or 1920, info["height"] or 1080,
                                          args.mode or style.get("layout", "blur")):
                    log.warning("[quality] %s", note)

            if time.time() - last_status >= 60:
                state = (f"recording {hms(media_duration(cur) or 0)}" if cur and is_growing(cur)
                         else "waiting for the stream")
                base = detector.baseline
                log.info("[status] %s · chat %d msgs/min (avg %s/10s) · spikes %d · clips %d",
                         state, minute_msgs, f"{base:.1f}" if base is not None else "–",
                         stats["spikes"], stats["clips"])
                silent_minutes = silent_minutes + 1 if minute_msgs == 0 else 0
                if silent_minutes >= 3 and not warned_silent:
                    warned_silent = True
                    log.warning("[chat] no chat messages for 3 minutes — clips come from chat spikes, so "
                                "nothing will be clipped here. The channel's chat may be closed or very "
                                "quiet (e.g. a rerun); try a channel with an active chat.")
                last_status, minute_msgs = time.time(), 0
    except KeyboardInterrupt:
        log.info("stopping — finishing the clips already detected (Ctrl+C again to quit now)")

    if rec.poll() is None:
        rec.terminate()
    try:
        rec.wait(timeout=15)
    except subprocess.TimeoutExpired:
        rec.kill()
    counter.stop()
    counts.close()
    spikes_csv.close()

    dispatch_ready(pending, jobs, rec_dir, force=True)
    jobs.put(None)
    try:
        worker.join()
    except KeyboardInterrupt:
        pass
    log.info("done · %d spikes · %d clips in %s", stats["spikes"], stats["clips"], clips_dir)


if __name__ == "__main__":
    main()
