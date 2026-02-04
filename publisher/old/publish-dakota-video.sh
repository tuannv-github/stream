#!/bin/bash
# Script to publish Dakota Johnson video to /stream/go2/front using GStreamer

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIDEO_FILE="$SCRIPT_DIR/assets/dakota-video.mp4"
TOPIC="/stream/go2/front"
SERVER="10.1.101.210"
PORT=1935
PROTOCOL="rtmp"

# Run the Python publisher script
python3 "$(dirname "$0")/publish-video-file.py" \
    "$VIDEO_FILE" \
    -s "$SERVER" \
    -p "$PORT" \
    -t "$TOPIC" \
    --protocol "$PROTOCOL" \
    --loop \
    "$@"
