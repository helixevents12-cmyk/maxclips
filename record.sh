#!/usr/bin/env bash
# record.sh — wait for a Twitch channel to go live, then record it from the first second.
#
#   ./record.sh <channel> [output_dir]
#
# How "from the first second" works: Twitch has no rewind, so we poll every POLL
# seconds while the channel is offline, and --hls-live-restart grabs the oldest
# segments still in the live playlist the moment the stream appears. With the
# default 5 s poll that covers the gap.
#
# If the stream drops mid-broadcast (streamer's internet, OBS crash), we keep
# watching for REJOIN_WAIT seconds and record the comeback as a new part file.
#
# Environment:
#   QUALITY      streamlink quality (default: best)
#   POLL         seconds between live checks while offline (default: 5)
#   REJOIN_WAIT  seconds to wait for a dropped stream to come back (default: 180)
#
# Written for macOS's bash 3.2 as well as newer bash.

CHANNEL="$1"
OUT="${2:-recordings}"
QUALITY="${QUALITY:-best}"
POLL="${POLL:-5}"
REJOIN_WAIT="${REJOIN_WAIT:-180}"

if [ -z "$CHANNEL" ]; then
  echo "usage: $0 <channel> [output_dir]" >&2
  exit 2
fi
if ! command -v streamlink >/dev/null 2>&1; then
  echo "[record] streamlink not found — pip install streamlink" >&2
  exit 1
fi
mkdir -p "$OUT"

# Streamlink 7+ always filters Twitch ads; older versions need the flag.
ADS_FLAG=""
if streamlink --help 2>/dev/null | grep -q -- "--twitch-disable-ads"; then
  ADS_FLAG="--twitch-disable-ads"
fi

SL_PID=""
cleanup() {
  if [ -n "$SL_PID" ]; then
    kill -TERM "$SL_PID" 2>/dev/null
    wait "$SL_PID" 2>/dev/null
  fi
  echo "[record] stopped"
  exit 0
}
trap cleanup INT TERM

count_parts() { ls "$OUT"/*.ts 2>/dev/null | wc -l | tr -d ' '; }

# Run streamlink in the background and wait, so a TERM from the launcher
# reaches it right away instead of after the stream ends.
record_once() {
  # shellcheck disable=SC2086
  streamlink "https://twitch.tv/$CHANNEL" "$QUALITY" \
    --retry-streams "$POLL" "$@" \
    --hls-live-restart \
    --stream-segment-threads 2 \
    $ADS_FLAG \
    --loglevel info \
    --output "$OUT/${CHANNEL}_{time:%Y%m%d-%H%M%S}.ts" &
  SL_PID=$!
  wait "$SL_PID"
  local rc=$?
  SL_PID=""
  return $rc
}

echo "[record] waiting for twitch.tv/$CHANNEL to go live (checking every ${POLL}s)"
record_once

while true; do
  before=$(count_parts)
  echo "[record] stream stopped — watching ${REJOIN_WAIT}s in case it comes back"
  record_once --retry-max $(( REJOIN_WAIT / POLL ))
  after=$(count_parts)
  if [ "$after" -le "$before" ]; then
    echo "[record] stream is over"
    break
  fi
done
exit 0
