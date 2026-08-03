#!/bin/bash

# Exit on error
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONDA_DIR="${CONDA_DIR:-$SCRIPT_DIR/miniconda3}"
ENV_NAME="stream-subscriber"
MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh"

echo "Updating package lists..."
APT_UPDATE_OPTS=()
if ! python3 -c "import apt_pkg" >/dev/null 2>&1; then
    echo "Warning: apt_pkg unavailable for $(python3 --version). Skipping apt post-update hooks."
    APT_UPDATE_OPTS=(-o APT::Update::Post-Invoke-Success::=)
fi
sudo apt-get "${APT_UPDATE_OPTS[@]}" update

echo "Installing system GStreamer plugins..."
sudo apt-get install -y \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-tools \
    gstreamer1.0-gl

if [ ! -f "$CONDA_DIR/bin/conda" ]; then
    echo "Downloading Miniconda to $CONDA_DIR..."
    INSTALLER="$(mktemp /tmp/miniconda.XXXXXX.sh)"
    curl -fsSL "$MINICONDA_URL" -o "$INSTALLER"
    bash "$INSTALLER" -b -p "$CONDA_DIR"
    rm -f "$INSTALLER"
else
    echo "Miniconda already installed at $CONDA_DIR"
fi

# shellcheck source=/dev/null
source "$CONDA_DIR/etc/profile.d/conda.sh"
conda config --set auto_activate_base false

# Newer Miniconda builds require explicit channel ToS acceptance.
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main >/dev/null 2>&1 || true
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r >/dev/null 2>&1 || true

echo "Creating conda environment '$ENV_NAME'..."
export PYTHONNOUSERSITE=1
if conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
    conda env update -n "$ENV_NAME" -f "$SCRIPT_DIR/environment.yml" --prune
else
    conda env create -f "$SCRIPT_DIR/environment.yml"
fi

echo "Making scripts executable..."
chmod +x "$SCRIPT_DIR/run.sh"

echo "Setup completed successfully!"
echo "Run the app with: ./run.sh"
