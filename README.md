# Maxclips — Twitch live auto-clipper

> Open-source tool that records a Twitch stream, detects chat-reaction spikes and turns them into captioned vertical (9:16) clips that credit the streamer. Python + ffmpeg + Whisper, runs locally on macOS/Linux.

One command waits for a channel to go live and records the whole broadcast. It also watches chat and turns every chat spike into a captioned 9:16 clip that credits the streamer.

```
python3 clipper.py <channel>
```

| Step | File | What it does |
|---|---|---|
| 1 | `record.sh` | Checks the channel every 5 s and records from the moment it goes live. If the stream drops, it waits 3 min for it to come back. |
| 2 | `chat_counter.py` | Reads chat anonymously over IRC as `justinfanNNNNN`, with no account or token. It counts messages in 10 s windows and leaves out bots like Nightbot. |
| 3 | `spikes.py` | Marks a spike when a window has more than 3× the rolling 5-minute average and at least 5 messages. Back-to-back hot windows count as one spike. |
| 4 | `cut.py` | Cuts 20 s before to 10 s after each spike into a 1080×1920 mp4, with `twitch.tv/<channel>` burned in at the top. |
| 5 | `captions.py` | Runs Whisper and burns in short captions of 1–3 words at a time. |

## Setup (macOS)

```bash
# ffmpeg with the text + subtitle filters (Homebrew's plain "ffmpeg" was slimmed down in 2026)
brew install ffmpeg-full
echo 'export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc

git clone https://github.com/<your-user>/maxclips && cd maxclips
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # streamlink + faster-whisper
chmod +x record.sh
```

When `clipper.py` starts, it checks for missing tools and tells you what to install.

## Permission first

Add a channel to `allowed_channels.txt` only once the streamer has said yes, or their posted clip policy allows reuse. Write where the permission came from after a `#`. `clipper.py` refuses to run for any channel that isn't on the list.

Every clip has `twitch.tv/<channel>` on screen. Each clip also gets a `.txt` file next to it with a credit caption you can paste when you post.

## Run

```bash
source .venv/bin/activate
python3 clipper.py somestreamer
```

You can start it hours before the stream. Press Ctrl+C to stop recording; it still finishes the clips it has already detected.

The output goes to `sessions/<channel>_<date-time>/`:

```
recordings/        the full broadcast (.ts), one file per part if the stream dropped
clips/             finished clips + credit .txt   ← these are the ones to post
work/              the same clips before captions
chat_counts.csv    messages per 10 s window
spikes.csv         every spike and where it was cut
clipper.log
```

## Style (style.json)

Default look (all configurable):
- The full gameplay frame sits on a blurred background with a navy tint (#1B2A5E), framed by cyan lines (#4DA8FF).
- The credit `twitch.tv/<channel>` sits at the top, below the TikTok/Reels tabs.
- The hook reads **WAIT FOR IT...** and switches to **CHAT WENT CRAZY** in cyan right at the moment.
- Captions are big, uppercase Montserrat Black. The word being spoken lights up in cyan.
- A cyan progress bar runs along the bottom, and every clip fades in and out.

Open `style.json` to change the colours, the hook text, the caption size or the layout. You can also add a `watermark` (e.g. your handle). Set either hook text to `""` to hide it.

The fonts are in `fonts/`: Montserrat, under the SIL Open Font License, which is free to use and share.

## Tuning

| Flag | Default | Use it when |
|---|---|---|
| `--factor` | 3 | Too many clips: raise it to 4–5. Too few: lower it to 2.5. |
| `--min-count` | 5 | Small chats trigger on noise: raise it. |
| `--pre` / `--post` | 20 / 10 | You want a different clip length or framing. |
| `--chat-delay` | 0 | Clips keep starting after the moment: try 3–5. |
| `--mode` | style.json | `crop` fills the screen with a centre crop and loses the sides. |
| `--model` | small | `medium` gives better captions but is slower; `tiny` is fastest. |
| `--language` | auto | Force a language, e.g. `sq`, `en`, `de`. |
| `--no-captions` | off | Skip Whisper. |
| `--quality` | best | e.g. `720p60` to save disk space. |

## Using the pieces on their own

```bash
./record.sh somestreamer recordings/
python3 chat_counter.py somestreamer --csv counts.csv
python3 spikes.py counts.csv                     # add --follow for a live file
python3 cut.py recordings/x.ts --start 754 --credit somestreamer -o clip.mp4
python3 captions.py clip.mp4 -o clip_final.mp4

# re-make a session's clips (new style, or move/lengthen a cut)
python3 restyle.py sessions/eslcs_20261117-232210 --list
python3 restyle.py sessions/eslcs_20261117-232210
python3 restyle.py sessions/eslcs_20261117-232210 --only 2 --shift -5 --length 40
```

## How the timing works

Chat time is wall-clock time, but a recording can drift from the wall clock because of the catch-up at stream start, filtered ad breaks, or dropped parts. So whenever a spike fires, the clipper measures back from the live end of the recording file. That keeps each cut lined up with the moment chat reacted to.

Whisper downloads its model the first time it runs (about 480 MB for `small`). The clipper loads it while it waits for the stream to start.

## License

Code: MIT (see `LICENSE`). Fonts in `fonts/`: Montserrat, SIL Open Font License (`fonts/OFL.txt`). The Maxclips logo in `brand/` is the project's own branding.

## Ethics

Only clip streamers who allow it, and always credit them. The tool enforces an allow-list (`allowed_channels.txt`).
