#!/bin/bash

# Exit on error
set -e

echo "Updating package lists..."
sudo apt-get update

echo "Installing system dependencies for GStreamer and PyGObject..."
sudo apt-get install -y \
    python3-pip \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gtk-3.0 \
    gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-tools \
    gstreamer1.0-gl \
    libgirepository1.0-dev \
    libcairo2-dev \
    pkg-config \
    python3-dev \
    python3-pyqt5 \
    pyqt5-dev-tools

echo "Installing Python dependencies from requirements file..."
# Using --break-system-packages because Debian 12+ restricts pip install to venvs by default
# This is acceptable for a dedicated development environment.
pip3 install --break-system-packages -r stream_subscriber.requirement.txt

echo "Making scripts executable..."
chmod +x run.sh

echo "Setup completed successfully!"
