#!/bin/bash

# Install dependencies for video-publisher.py
# GStreamer, Python GI bindings, V4L2 utils, and hardware encoder support

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================="
echo "Video Publisher - Installation"
echo "=========================================="
echo ""

echo "Updating package lists..."
APT_UPDATE_OPTS=()
if ! python3 -c "import apt_pkg" >/dev/null 2>&1; then
    echo "Warning: apt_pkg unavailable for $(python3 --version). Skipping apt post-update hooks."
    APT_UPDATE_OPTS=(-o APT::Update::Post-Invoke-Success::=)
fi
sudo apt-get "${APT_UPDATE_OPTS[@]}" update

echo ""
echo "Installing Python 3 and GObject introspection..."
sudo apt-get install -y \
    python3 \
    python3-dev \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0

echo ""
echo "Installing GStreamer core and plugins..."
sudo apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-rtsp \
    gstreamer1.0-nice \
    gstreamer1.0-gl \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgstreamer-plugins-good1.0-dev \
    libgstreamer-plugins-bad1.0-dev

echo ""
echo "Installing V4L2 and hardware encoder support..."
sudo apt-get install -y \
    v4l-utils \
    libv4l-dev \
    gstreamer1.0-vaapi \
    libva-dev \
    libva-drm2 \
    libva-x11-2 \
    vainfo || true

# Jetson NVIDIA plugins (available on JetPack images; ignore if missing)
sudo apt-get install -y gstreamer1.0-plugins-nv 2>/dev/null || true

echo ""
echo "Making scripts executable..."
chmod +x "$SCRIPT_DIR/video-publisher.py"
chmod +x "$SCRIPT_DIR/utils/check-hw-encoders.sh"

echo ""
echo "=========================================="
echo "Verifying installation..."
echo "=========================================="

echo ""
echo "Python: $(python3 --version 2>&1)"

echo ""
echo "GStreamer:"
gst-inspect-1.0 --version

echo ""
echo "Checking Python GStreamer bindings..."
if python3 -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); print(f'✓ GStreamer Python bindings: {Gst.version_string()}')"; then
    :
else
    echo "✗ GStreamer Python bindings not found"
fi

echo ""
echo "Checking required plugins..."
for plugin in v4l2src x264enc h264parse rtph264pay udpsink flvmux rtmp2sink; do
    if gst-inspect-1.0 "$plugin" >/dev/null 2>&1; then
        echo "✓ $plugin"
    else
        echo "✗ $plugin not found"
    fi
done

echo ""
echo "Checking V4L2 utilities..."
if command -v v4l2-ctl >/dev/null 2>&1; then
    echo "✓ v4l2-ctl installed"
else
    echo "✗ v4l2-ctl not found"
fi

echo ""
echo "=========================================="
echo "Installation complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "  1. Check hardware encoders:  ./utils/check-hw-encoders.sh"
echo "  2. List video devices:       python3 video-publisher.py -l"
echo "  3. Publish (interactive):    python3 video-publisher.py"
echo "  4. Publish (explicit):       python3 video-publisher.py -s /dev/video0 -d go2-front"
echo ""
