#!/usr/bin/env python3
"""
chat_counter.py — count Twitch chat messages in fixed 10-second windows.

Connects anonymously to Twitch IRC (nick justinfanNNNNN, no account, no token),
joins #<channel> read-only and emits one row per window, including empty ones.

    python3 chat_counter.py <channel>                       # print to the terminal
    python3 chat_counter.py <channel> --csv chat_counts.csv # also append to a CSV

Windows are aligned to the wall clock (21:40:00-21:40:10, ...) and use local
receive time, so they line up with the recorder running on the same machine.
A window during which the connection was down is marked complete=0, so the
spike detector skips it instead of reading it as "chat went quiet".
"""
from __future__ import annotations

import argparse
import os
import random
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

DEFAULT_HOST = "irc.chat.twitch.tv"
TLS_PORT = 6697

# Bots post on timers and commands, not in reaction to the stream.
DEFAULT_IGNORED = {
    "nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot",
    "sery_bot", "soundalerts", "botrixoficial", "kofistreambot", "blerp",
    "pokemoncommunitygame", "commanderroot",
}


@dataclass
class Window:
    start: float      # epoch seconds, inclusive
    end: float        # epoch seconds, exclusive
    count: int        # chat messages received in the window
    complete: bool    # False if the IRC connection was down for any part of it

    @property
    def utc(self) -> str:
        return datetime.fromtimestamp(self.start, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_irc(line: str):
    """Split one raw IRC line into (tags, prefix, command, params)."""
    tags = {}
    if line.startswith("@"):
        raw, _, line = line[1:].partition(" ")
        for kv in raw.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    trailing = None
    if " :" in line:
        line, _, trailing = line.partition(" :")
    parts = line.split()
    command = parts[0] if parts else ""
    params = parts[1:]
    if trailing is not None:
        params.append(trailing)
    return tags, prefix, command, params


def room_modes(tags: dict) -> list:
    """Chat restrictions announced in a ROOMSTATE line."""
    modes = []
    if tags.get("subs-only") == "1":
        modes.append("subscribers-only")
    if tags.get("emote-only") == "1":
        modes.append("emote-only")
    fo = tags.get("followers-only")
    if fo not in (None, "", "-1"):
        modes.append("followers-only" + (f" ({fo} min)" if fo != "0" else ""))
    if tags.get("slow") not in (None, "", "0"):
        modes.append(f"slow mode {tags['slow']}s")
    return modes


class ChatCounter:
    def __init__(self, channel: str, window: int = 10, host: str = DEFAULT_HOST,
                 port: int = TLS_PORT, tls: bool = True, ignored=DEFAULT_IGNORED,
                 log: Optional[Callable[[str], None]] = None):
        self.channel = channel.lower().lstrip("#")
        self.window = window
        self.host, self.port, self.tls = host, port, tls
        self.ignored = {n.lower() for n in ignored}
        self.log = log or (lambda m: print(f"[chat] {m}", file=sys.stderr, flush=True))
        self.nick = f"justinfan{random.randint(10000, 99999)}"
        self.stop_event = threading.Event()
        self.total = 0
        self._joined = False
        self._idx: Optional[int] = None   # index of the window being filled
        self._count = 0
        self._clean = False               # connected for the whole current window

    def stop(self):
        self.stop_event.set()

    # ---- window clock -------------------------------------------------
    def _tick(self, on_window, now: Optional[float] = None):
        now = time.time() if now is None else now
        idx = int(now // self.window)
        if self._idx is None:             # first window is partial: never "complete"
            self._idx, self._count, self._clean = idx, 0, False
            return
        while idx > self._idx:
            start = self._idx * self.window
            on_window(Window(start, start + self.window, self._count, self._clean))
            self._idx += 1
            self._count = 0
            self._clean = self._joined

    # ---- connection ---------------------------------------------------
    def run(self, on_window: Callable[[Window], None]):
        """Count until stop() is called. Reconnects with backoff on any drop."""
        backoff = 1
        while not self.stop_event.is_set():
            try:
                if self._session(on_window):
                    backoff = 1
            except (OSError, ConnectionError) as e:
                if not self.stop_event.is_set():
                    self.log(f"disconnected ({e}); reconnecting in {backoff}s")
            self._joined = False
            self._clean = False
            deadline = time.time() + backoff
            while time.time() < deadline and not self.stop_event.is_set():
                self._tick(on_window)
                time.sleep(0.25)
            backoff = min(backoff * 2, 60)

    def _session(self, on_window) -> bool:
        sock = socket.create_connection((self.host, self.port), timeout=15)
        if self.tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        sock.settimeout(0.5)

        def send(line: str):
            sock.sendall((line + "\r\n").encode("utf-8"))

        try:
            send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            send("PASS SCHMOOPIIE")          # any pass works for justinfan logins
            send(f"NICK {self.nick}")
            send(f"JOIN #{self.channel}")
            buf, last_rx = b"", time.time()
            while not self.stop_event.is_set():
                self._tick(on_window)
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    if time.time() - last_rx > 360:   # Twitch PINGs every ~5 min
                        raise ConnectionError("no data for 6 minutes")
                    continue
                if not data:
                    raise ConnectionError("server closed the connection")
                last_rx = time.time()
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for raw in lines:
                    if raw:
                        self._handle(raw.decode("utf-8", "replace"), send)
            return self._joined
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _handle(self, line: str, send):
        tags, prefix, cmd, params = parse_irc(line)
        if cmd == "PING":
            send("PONG :" + (params[-1] if params else "tmi.twitch.tv"))
        elif cmd == "PRIVMSG":
            login = prefix.split("!", 1)[0].lower()
            if login not in self.ignored:
                self._count += 1
                self.total += 1
        elif cmd in ("366", "ROOMSTATE"):
            if not self._joined:
                self._joined = True
                self.log(f"joined #{self.channel} as {self.nick} (anonymous, read-only)")
            if cmd == "ROOMSTATE":
                modes = room_modes(tags)
                if modes:
                    self.log("chat is restricted: " + ", ".join(modes) + " — fewer messages to count")
        elif cmd == "RECONNECT":
            raise ConnectionError("Twitch asked us to reconnect")
        elif cmd == "NOTICE" and params:
            self.log(f"notice: {params[-1]}")


def make_counter(channel: str, window: int = 10, irc: Optional[str] = None, log=None) -> ChatCounter:
    """irc='host:port' (or env CLIPPER_IRC) points at a plain-text test server instead of Twitch."""
    irc = irc or os.environ.get("CLIPPER_IRC")
    if irc:
        host, _, port = irc.rpartition(":")
        return ChatCounter(channel, window, host=host, port=int(port), tls=False, log=log)
    return ChatCounter(channel, window, log=log)


class CountsCsv:
    """Append windows to a CSV: start,end,utc,count,complete."""

    def __init__(self, path):
        self.path = Path(path)
        new = not self.path.exists() or self.path.stat().st_size == 0
        self.f = open(self.path, "a", buffering=1)
        if new:
            self.f.write("start,end,utc,count,complete\n")

    def write(self, w: Window):
        self.f.write(f"{w.start:.0f},{w.end:.0f},{w.utc},{w.count},{int(w.complete)}\n")

    def close(self):
        self.f.close()


def main():
    ap = argparse.ArgumentParser(description="Count Twitch chat messages per window (anonymous, read-only).")
    ap.add_argument("channel")
    ap.add_argument("--window", type=int, default=10, help="window length in seconds (default 10)")
    ap.add_argument("--csv", help="also append windows to this CSV file")
    ap.add_argument("--irc", metavar="HOST:PORT", help=argparse.SUPPRESS)  # plain-text test server
    args = ap.parse_args()

    out = CountsCsv(args.csv) if args.csv else None

    def on_window(w: Window):
        flag = "" if w.complete else "  (incomplete)"
        print(f"{w.utc} UTC  {w.count:5d} msgs{flag}", flush=True)
        if out:
            out.write(w)

    counter = make_counter(args.channel, args.window, args.irc)
    try:
        counter.run(on_window)
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.close()


if __name__ == "__main__":
    main()
