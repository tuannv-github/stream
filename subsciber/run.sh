#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONDA_DIR="${CONDA_DIR:-$SCRIPT_DIR/miniconda3}"
ENV_NAME="stream-subscriber"

if [ ! -f "$CONDA_DIR/etc/profile.d/conda.sh" ]; then
    echo "Conda not found at $CONDA_DIR. Run ./setup.sh first." >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$CONDA_DIR/etc/profile.d/conda.sh"
conda activate "$ENV_NAME"

# ROS/shell env vars can pull in incompatible GLib, GStreamer, and Qt libraries.
unset PYTHONPATH
export PYTHONNOUSERSITE=1
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib"
export GI_TYPELIB_PATH="$CONDA_PREFIX/lib/girepository-1.0"
export GST_PLUGIN_SYSTEM_PATH="$CONDA_PREFIX/lib/gstreamer-1.0"
export QT_PLUGIN_PATH="$CONDA_PREFIX/plugins"
unset QT_QPA_PLATFORM_PLUGIN_PATH

exec python "$SCRIPT_DIR/stream_subscriber.py" "$@"
