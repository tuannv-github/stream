#!/bin/bash

# Complete installation script for Video Streaming System
# Installs all dependencies using apt install

set -e  # Exit on error

echo "=========================================="
echo "Video Streaming System - Installation"
echo "=========================================="
echo ""

# Update package list
echo "Updating package list..."
sudo apt update

# Install Python 3 and pip
echo ""
echo "Installing Python 3 and pip..."
sudo apt install -y \
    python3 \
    python3-pip \
    python3-dev

# Install GStreamer core libraries
echo ""
echo "Installing GStreamer core libraries..."
sudo apt install -y \
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

# Install additional GStreamer plugins
echo ""
echo "Installing additional GStreamer plugins..."
sudo apt install -y \
    gstreamer1.0-x \
    gstreamer1.0-alsa \
    gstreamer1.0-gl \
    gstreamer1.0-gtk3 \
    gstreamer1.0-qt5 \
    gstreamer1.0-pulseaudio \
    gstreamer1.0-rtsp \
    gstreamer1.0-nice

# Install hardware encoder support packages
echo ""
echo "Installing hardware encoder support..."
sudo apt install -y \
    gstreamer1.0-vaapi \
    libva-dev \
    libva-drm2 \
    libva-x11-2 \
    vainfo \
    v4l-utils \
    libv4l-dev || true

# Install PyQt5 and dependencies
echo ""
echo "Installing PyQt5 and GUI dependencies..."
sudo apt install -y \
    python3-pyqt5 \
    python3-pyqt5.qtopengl \
    pyqt5-dev-tools

# Install Python GObject introspection (for GStreamer Python bindings)
echo ""
echo "Installing Python GObject introspection..."
sudo apt install -y \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0

# Install additional utilities
echo ""
echo "Installing additional utilities..."
sudo apt install -y \
    curl \
    wget \
    git

# Verify installations
echo ""
echo "=========================================="
echo "Verifying installations..."
echo "=========================================="

# Check Python
echo ""
echo "Python version:"
python3 --version

# Check GStreamer
echo ""
echo "GStreamer version:"
gst-inspect-1.0 --version

# Check PyQt5
echo ""
echo "Checking PyQt5 installation:"
python3 -c "from PyQt5.QtCore import QT_VERSION_STR; print(f'PyQt5 version: {QT_VERSION_STR}')" 2>/dev/null && echo "✓ PyQt5 installed" || echo "✗ PyQt5 not found"

# Check GStreamer Python bindings
echo ""
echo "Checking GStreamer Python bindings:"
python3 -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; print(f'✓ GStreamer Python bindings: {Gst.version_string()}')" 2>/dev/null && echo "✓ GStreamer Python bindings installed" || echo "✗ GStreamer Python bindings not found"

# Check V4L2 utilities
echo ""
echo "Checking V4L2 utilities:"
if command -v v4l2-ctl > /dev/null 2>&1; then
    v4l2-ctl --version && echo "✓ V4L2 utilities installed"
else
    echo "✗ V4L2 utilities not found"
fi

echo "Installing influxdb-client Python package..."
pip3 install --break-system-packages influxdb-client && echo "✓ influxdb-client installed" || echo "✗ Failed to install influxdb-client"

echo ""
echo "=========================================="
echo "Installation complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Check hardware encoders: cd publisher && ./check-hw-encoders.sh"
echo "2. List video devices: python3 publisher/video-publisher.py -l"
echo "3. Clear InfluxDB bucket (if needed): python3 utils/clear_bucket.py fcclab"
echo ""
