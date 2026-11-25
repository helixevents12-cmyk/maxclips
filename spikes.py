#!/usr/bin/env python3
"""
spikes.py — flag chat windows that run hot.

A window is a spike when
    count > FACTOR x (average of the previous HISTORY windows)      default 3x over 30 windows (5 min)
    and count >= MIN_COUNT                                           so 0 -> 2 messages in a dead chat isn't a spike
    and at least WARMUP windows of history exist                     default 6 (1 min)
Back-to-back hot windows count as one spike, anchored at the first one, and a new
spike must start at least COOLDOWN seconds after the last, so clips never overlap.

    python3 spikes.py chat_counts.csv            # list spikes in a recorded session
    python3 spikes.py chat_counts.csv --follow   # watch a live session's CSV
"""
from __future__ import annotations

import argparse
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional, Tuple


@dataclass
class Spike:
    start: float       # epoch seconds — start of the first hot window
    end: float
    count: int
    baseline: float    # rolling average at the time

    @property
    def ratio(self) -> float:
        return self.count / self.baseline if self.baseline > 0 else float("inf")

    @property
    def utc(self) -> str:
        return datetime.fromtimestamp(self.start, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SpikeDetector:
    def __init__(self, factor: float = 3.0, history: int = 30, warmup: int = 6,
                 min_count: int = 5, cooldown: float = 30.0):
        self.factor = factor
        self.warmup = warmup
        self.min_count = min_count
        self.cooldown = cooldown
        self.hist: deque = deque(maxlen=history)
        self._in_event = False
        self._last_start = float("-inf")

    @property
    def baseline(self) -> Optional[float]:
        return sum(self.hist) / len(self.hist) if self.hist else None

    def update(self, start: float, end: float, count: int, complete: bool = True) -> Optional[Spike]:
        """Feed one window; returns a Spike when a new one begins."""
        if not complete:              # connection dropped during this window: no information
            return None
        spike = None
        hot = False
        if len(self.hist) >= self.warmup:
            base = self.baseline
            hot = count >= self.min_count and count > self.factor * base
            if hot and not self._in_event and start - self._last_start >= self.cooldown:
                spike = Spike(start, end, count, base)
                self._last_start = start
        self._in_event = hot
        self.hist.append(count)
        return spike


def read_counts(path: str, follow: bool = False) -> Iterator[Tuple[float, float, int, bool]]:
    """Yield (start, end, count, complete) rows from a chat_counter CSV."""
    with open(path) as f:
        f.readline()                                   # header
        while True:
            pos = f.tell()
            line = f.readline()
            if not line or not line.endswith("\n"):    # end of file or half-written row
                if not follow:
                    return
                f.seek(pos)
                time.sleep(1)
                continue
            start, end, _utc, count, complete = line.strip().split(",")
            yield float(start), float(end), int(count), complete == "1"


def main():
    ap = argparse.ArgumentParser(description="Find chat spikes in a chat_counter CSV.")
    ap.add_argument("csv")
    ap.add_argument("--factor", type=float, default=3.0, help="spike = above FACTOR x rolling average (default 3)")
    ap.add_argument("--history", type=int, default=30, help="windows in the rolling average (default 30)")
    ap.add_argument("--warmup", type=int, default=6, help="windows needed before flagging (default 6)")
    ap.add_argument("--min-count", type=int, default=5, help="ignore windows below this many messages (default 5)")
    ap.add_argument("--cooldown", type=float, default=30, help="seconds between spikes (default 30)")
    ap.add_argument("--follow", action="store_true", help="keep watching the file as it grows")
    args = ap.parse_args()

    det = SpikeDetector(args.factor, args.history, args.warmup, args.min_count, args.cooldown)
    n = 0
    try:
        for start, end, count, complete in read_counts(args.csv, args.follow):
            s = det.update(start, end, count, complete)
            if s:
                n += 1
                print(f"SPIKE {s.utc} UTC  {s.count} msgs/10s  avg {s.baseline:.1f}  ({s.ratio:.1f}x)", flush=True)
    except KeyboardInterrupt:
        pass
    print(f"{n} spike(s)")


if __name__ == "__main__":
    main()
