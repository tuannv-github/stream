#!/bin/bash

# GStreamer installation script for UDP video publishing
# This script installs GStreamer and necessary plugins for video streaming over UDP

set -e  # Exit on error

echo "Installing GStreamer and plugins for UDP video publishing..."

# Update package list
sudo apt-get update

# Install GStreamer core libraries
sudo apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgstreamer-plugins-good1.0-dev \
    libgstreamer-plugins-bad1.0-dev

# Install additional codecs and plugins for video encoding/streaming
sudo apt-get install -y \
    gstreamer1.0-x \
    gstreamer1.0-alsa \
    gstreamer1.0-gl \
    gstreamer1.0-gtk3 \
    gstreamer1.0-qt5 \
    gstreamer1.0-pulseaudio

# Install RTP plugins for UDP streaming
sudo apt-get install -y \
    gstreamer1.0-rtsp \
    gstreamer1.0-nice

# Install hardware encoder support packages
# NVENC (NVIDIA) - for Jetson/Tegra devices
sudo apt-get install -y \
    gstreamer1.0-plugins-bad \
    libgstreamer-plugins-bad1.0-dev || true

# VAAPI (Intel/AMD) hardware acceleration
sudo apt-get install -y \
    gstreamer1.0-vaapi \
    libva-dev \
    libva-drm2 \
    libva-x11-2 \
    vainfo || true

# V4L2 hardware encoder support (for various platforms)
sudo apt-get install -y \
    v4l-utils \
    libv4l-dev || true

# Verify installation
echo ""
echo "Verifying GStreamer installation..."
gst-inspect-1.0 --version

echo ""
echo "Checking for UDP-related plugins..."
echo "Checking udpsink plugin..."
gst-inspect-1.0 udpsink > /dev/null 2>&1 && echo "✓ udpsink plugin found" || echo "✗ udpsink plugin not found"

echo "Checking rtp plugins..."
gst-inspect-1.0 rtpvrawpay > /dev/null 2>&1 && echo "✓ rtpvrawpay plugin found" || echo "✗ rtpvrawpay plugin not found"
gst-inspect-1.0 rtph264pay > /dev/null 2>&1 && echo "✓ rtph264pay plugin found" || echo "✗ rtph264pay plugin not found"
gst-inspect-1.0 rtph265pay > /dev/null 2>&1 && echo "✓ rtph265pay plugin found" || echo "✗ rtph265pay plugin not found"

echo "Checking video encoders..."
gst-inspect-1.0 x264enc > /dev/null 2>&1 && echo "✓ x264enc plugin found" || echo "✗ x264enc plugin not found"
gst-inspect-1.0 vp8enc > /dev/null 2>&1 && echo "✓ vp8enc plugin found" || echo "✗ vp8enc plugin not found"

echo ""
echo "Checking hardware video encoder support..."
echo ""

# Check for Jetson/Tegra hardware encoders (nvv4l2*)
echo "=== Jetson/Tegra Hardware Encoders (nvv4l2*) ==="

# Check system hardware encoder device
echo "System Hardware Encoder Device:"
if [ -e /dev/nvhost-msenc ]; then
    echo "  ✓ /dev/nvhost-msenc found"
    if [ -r /dev/nvhost-msenc ] && [ -w /dev/nvhost-msenc ]; then
        echo "    ✓ Device is accessible"
    else
        echo "    ⚠ Device exists but may have permission issues"
    fi
else
    echo "  ✗ /dev/nvhost-msenc not found"
fi

# Check device model
if [ -r /proc/device-tree/model ]; then
    MODEL=$(cat /proc/device-tree/model 2>/dev/null | tr -d '\0')
    echo "  Device Model: $MODEL"
fi

echo ""

# Check plugin availability
if gst-inspect-1.0 nvv4l2h264enc > /dev/null 2>&1; then
    echo "✓ nvv4l2h264enc (Jetson H.264) plugin found"
    gst-inspect-1.0 nvv4l2h264enc | grep -E "Factory Details|Long-name" | head -2
    
    # Check supported formats
    echo "  Supported input formats:"
    gst-inspect-1.0 nvv4l2h264enc | grep -A 1 "format:" | grep -oE "(I420|NV12|P010|Y444|NV24)" | sort -u | sed 's/^/    - /' || echo "    (check with: gst-inspect-1.0 nvv4l2h264enc)"
    
    # Test encoder capability
    echo "  Testing encoder capability..."
    if timeout 3 gst-launch-1.0 videotestsrc num-buffers=1 ! nvvidconv ! nvv4l2h264enc ! fakesink 2>&1 | grep -q "Setting pipeline to PLAYING"; then
        echo "    ✓ Encoder test: SUCCESS"
    else
        echo "    ? Encoder test: Could not verify (may need proper format conversion)"
    fi
else
    echo "✗ nvv4l2h264enc plugin not found"
fi

if gst-inspect-1.0 nvv4l2h265enc > /dev/null 2>&1; then
    echo "✓ nvv4l2h265enc (Jetson H.265/HEVC) plugin found"
    gst-inspect-1.0 nvv4l2h265enc | grep -E "Factory Details|Long-name" | head -2
    
    # Check supported formats
    echo "  Supported input formats:"
    gst-inspect-1.0 nvv4l2h265enc | grep -A 1 "format:" | grep -oE "(I420|NV12|P010|Y444|NV24)" | sort -u | sed 's/^/    - /' || echo "    (check with: gst-inspect-1.0 nvv4l2h265enc)"
else
    echo "✗ nvv4l2h265enc plugin not found"
fi

if gst-inspect-1.0 nvv4l2vp9enc > /dev/null 2>&1; then
    echo "✓ nvv4l2vp9enc (Jetson VP9) plugin found"
    gst-inspect-1.0 nvv4l2vp9enc | grep -E "Factory Details|Long-name" | head -2
else
    echo "✗ nvv4l2vp9enc plugin not found"
fi

if gst-inspect-1.0 nvv4l2av1enc > /dev/null 2>&1; then
    echo "✓ nvv4l2av1enc (Jetson AV1) plugin found"
    gst-inspect-1.0 nvv4l2av1enc | grep -E "Factory Details|Long-name" | head -2
else
    echo "✗ nvv4l2av1enc plugin not found"
fi

# Check for NVENC (NVIDIA desktop GPU hardware encoder)
echo ""
echo "=== NVIDIA NVENC (Desktop GPU Hardware Encoder) ==="
if gst-inspect-1.0 nv264enc > /dev/null 2>&1; then
    echo "✓ nv264enc (NVIDIA H.264) plugin found"
    gst-inspect-1.0 nv264enc | grep -E "Factory Details|element" | head -2
else
    echo "✗ nv264enc plugin not found"
fi

if gst-inspect-1.0 nvh264enc > /dev/null 2>&1; then
    echo "✓ nvh264enc (NVIDIA H.264) plugin found"
    gst-inspect-1.0 nvh264enc | grep -E "Factory Details|element" | head -2
else
    echo "✗ nvh264enc plugin not found"
fi

if gst-inspect-1.0 nvh265enc > /dev/null 2>&1; then
    echo "✓ nvh265enc (NVIDIA H.265/HEVC) plugin found"
    gst-inspect-1.0 nvh265enc | grep -E "Factory Details|element" | head -2
else
    echo "✗ nvh265enc plugin not found"
fi

# Check for VAAPI (Intel/AMD hardware encoder)
echo ""
echo "=== VAAPI (Intel/AMD Hardware Encoder) ==="
if gst-inspect-1.0 vaapih264enc > /dev/null 2>&1; then
    echo "✓ vaapih264enc plugin found"
    gst-inspect-1.0 vaapih264enc | grep -E "Factory Details|element" | head -2
else
    echo "✗ vaapih264enc plugin not found"
fi

if gst-inspect-1.0 vaapih265enc > /dev/null 2>&1; then
    echo "✓ vaapih265enc plugin found"
    gst-inspect-1.0 vaapih265enc | grep -E "Factory Details|element" | head -2
else
    echo "✗ vaapih265enc plugin not found"
fi

# Check VAAPI info if available
if command -v vainfo > /dev/null 2>&1; then
    echo ""
    echo "VAAPI hardware capabilities:"
    vainfo 2>/dev/null | grep -E "VAProfile|VAEntrypoint" | head -10 || echo "  (run 'vainfo' manually for details)"
fi

# Check for V4L2 hardware encoder (common on embedded platforms)
echo ""
echo "=== V4L2 Hardware Encoder ==="
if gst-inspect-1.0 v4l2h264enc > /dev/null 2>&1; then
    echo "✓ v4l2h264enc plugin found"
    gst-inspect-1.0 v4l2h264enc | grep -E "Factory Details|element" | head -2
else
    echo "✗ v4l2h264enc plugin not found"
fi

if gst-inspect-1.0 v4l2h265enc > /dev/null 2>&1; then
    echo "✓ v4l2h265enc plugin found"
    gst-inspect-1.0 v4l2h265enc | grep -E "Factory Details|element" | head -2
else
    echo "✗ v4l2h265enc plugin not found"
fi

# Check for V4L2 devices
if command -v v4l2-ctl > /dev/null 2>&1; then
    echo ""
    echo "V4L2 video devices:"
    v4l2-ctl --list-devices 2>/dev/null | head -20 || echo "  (run 'v4l2-ctl --list-devices' manually for details)"
    
    # Check for encoder-capable devices
    echo ""
    echo "Checking for encoder-capable V4L2 devices:"
    ENCODER_DEVICES=0
    for dev in /dev/video*; do
        if [ -e "$dev" ]; then
            CAPS=$(v4l2-ctl --device="$dev" --all 2>&1 | grep -i "capabilities" | grep -i "encoder")
            if [ -n "$CAPS" ]; then
                echo "  ✓ $dev appears to support encoding"
                ENCODER_DEVICES=$((ENCODER_DEVICES + 1))
            fi
        fi
    done
    if [ "$ENCODER_DEVICES" -eq 0 ]; then
        echo "  (No V4L2 encoder devices found - Jetson encoders use nvhost-msenc device)"
    fi
fi

# Check for OMX hardware encoder (common on embedded platforms)
echo ""
echo "=== OMX Hardware Encoder ==="
if gst-inspect-1.0 omxh264enc > /dev/null 2>&1; then
    echo "✓ omxh264enc plugin found"
    gst-inspect-1.0 omxh264enc | grep -E "Factory Details|element" | head -2
else
    echo "✗ omxh264enc plugin not found"
fi

if gst-inspect-1.0 omxh265enc > /dev/null 2>&1; then
    echo "✓ omxh265enc plugin found"
    gst-inspect-1.0 omxh265enc | grep -E "Factory Details|element" | head -2
else
    echo "✗ omxh265enc plugin not found"
fi

# List all available encoders
echo ""
echo "=== All Available Video Encoders ==="
gst-inspect-1.0 | grep -i "enc" | grep -E "h264|h265|hevc|264|265" | sort | head -20

echo ""
echo "GStreamer installation complete!"
echo ""
echo "Example commands to publish video via UDP:"
echo ""
echo "=== Software Encoders ==="
echo "1. Test pattern to UDP (H.264, software):"
echo "   gst-launch-1.0 videotestsrc ! x264enc ! rtph264pay ! udpsink host=127.0.0.1 port=5000"
echo ""
echo "2. Webcam to UDP (H.264, software):"
echo "   gst-launch-1.0 v4l2src device=/dev/video0 ! video/x-raw,width=640,height=480 ! x264enc ! rtph264pay ! udpsink host=127.0.0.1 port=5000"
echo ""
echo "=== Jetson Hardware Encoders (if available) ==="
echo "3. Test pattern to UDP (Jetson H.264 hardware):"
echo "   gst-launch-1.0 videotestsrc ! nvv4l2h264enc ! rtph264pay ! udpsink host=127.0.0.1 port=5000"
echo ""
echo "4. Webcam to UDP (Jetson H.264 hardware):"
echo "   gst-launch-1.0 v4l2src device=/dev/video0 ! video/x-raw,width=640,height=480 ! nvv4l2h264enc ! rtph264pay ! udpsink host=127.0.0.1 port=5000"
echo ""
echo "5. Test pattern to UDP (Jetson H.265 hardware):"
echo "   gst-launch-1.0 videotestsrc ! nvv4l2h265enc ! rtph265pay ! udpsink host=127.0.0.1 port=5000"
echo ""
echo "=== Other Hardware Encoders ==="
echo "6. Test pattern to UDP (VAAPI H.264):"
echo "   gst-launch-1.0 videotestsrc ! vaapih264enc ! rtph264pay ! udpsink host=127.0.0.1 port=5000"
echo ""
echo "7. Test pattern to UDP (V4L2 H.264):"
echo "   gst-launch-1.0 videotestsrc ! v4l2h264enc ! rtph264pay ! udpsink host=127.0.0.1 port=5000"
echo ""
echo "Note: Replace '127.0.0.1' with the target IP address and adjust port as needed."
