#!/bin/bash

# Quick script to check hardware video encoder support
# This script checks what hardware encoders are available without installing anything

echo "Hardware Video Encoder Support Check"
echo "===================================="
echo ""

# Check if GStreamer is installed
if ! command -v gst-inspect-1.0 > /dev/null 2>&1; then
    echo "⚠ GStreamer is not installed. Run ./install-gst.sh first."
    exit 1
fi

echo "GStreamer version:"
gst-inspect-1.0 --version
echo ""

# Check for Jetson/Tegra hardware encoders (nvv4l2*)
echo "=== Jetson/Tegra Hardware Encoders (nvv4l2*) ==="

# Check system hardware encoder device
echo "System Hardware Encoder Device:"
if [ -e /dev/nvhost-msenc ]; then
    echo "  ✓ /dev/nvhost-msenc found"
    ls -l /dev/nvhost-msenc | sed 's/^/    /'
    # Check permissions
    if [ -r /dev/nvhost-msenc ] && [ -w /dev/nvhost-msenc ]; then
        echo "    ✓ Device is readable and writable"
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
    echo "  Details:"
    gst-inspect-1.0 nvv4l2h264enc | grep -E "Factory Details|Long-name" | head -2 | sed 's/^/    /'
    
    # Check supported formats
    echo "  Supported input formats:"
    gst-inspect-1.0 nvv4l2h264enc | grep -A 1 "format:" | grep -oE "(I420|NV12|P010|Y444|NV24)" | sort -u | sed 's/^/    - /' || echo "    (check with: gst-inspect-1.0 nvv4l2h264enc)"
    
    # Test encoder capability
    echo "  Testing encoder capability..."
    if timeout 3 gst-launch-1.0 videotestsrc num-buffers=1 ! nvvidconv ! nvv4l2h264enc ! fakesink 2>&1 | grep -q "Setting pipeline to PLAYING"; then
        echo "    ✓ Encoder test: SUCCESS (can encode)"
    else
        ERROR=$(timeout 3 gst-launch-1.0 videotestsrc num-buffers=1 ! nvvidconv ! nvv4l2h264enc ! fakesink 2>&1 | grep -E "ERROR|WARNING.*could not|WARNING.*can't handle" | head -1)
        if [ -n "$ERROR" ]; then
            echo "    ⚠ Encoder test: May have issues"
            echo "      $ERROR" | sed 's/^/      /'
        else
            echo "    ? Encoder test: Could not verify (may need proper format conversion)"
        fi
    fi
else
    echo "✗ nvv4l2h264enc plugin not found"
fi

if gst-inspect-1.0 nvv4l2h265enc > /dev/null 2>&1; then
    echo "✓ nvv4l2h265enc (Jetson H.265/HEVC) plugin found"
    echo "  Details:"
    gst-inspect-1.0 nvv4l2h265enc | grep -E "Factory Details|Long-name" | head -2 | sed 's/^/    /'
    
    # Check supported formats
    echo "  Supported input formats:"
    gst-inspect-1.0 nvv4l2h265enc | grep -A 1 "format:" | grep -oE "(I420|NV12|P010|Y444|NV24)" | sort -u | sed 's/^/    - /' || echo "    (check with: gst-inspect-1.0 nvv4l2h265enc)"
else
    echo "✗ nvv4l2h265enc plugin not found"
fi

if gst-inspect-1.0 nvv4l2vp9enc > /dev/null 2>&1; then
    echo "✓ nvv4l2vp9enc (Jetson VP9) plugin found"
    echo "  Details:"
    gst-inspect-1.0 nvv4l2vp9enc | grep -E "Factory Details|Long-name" | head -2 | sed 's/^/    /'
else
    echo "✗ nvv4l2vp9enc plugin not found"
fi

if gst-inspect-1.0 nvv4l2av1enc > /dev/null 2>&1; then
    echo "✓ nvv4l2av1enc (Jetson AV1) plugin found"
    echo "  Details:"
    gst-inspect-1.0 nvv4l2av1enc | grep -E "Factory Details|Long-name" | head -2 | sed 's/^/    /'
else
    echo "✗ nvv4l2av1enc plugin not found"
fi

# Check for NVENC (NVIDIA desktop GPU hardware encoder)
echo ""
echo "=== NVIDIA NVENC (Desktop GPU Hardware Encoder) ==="
if gst-inspect-1.0 nv264enc > /dev/null 2>&1; then
    echo "✓ nv264enc (NVIDIA H.264) plugin found"
    echo "  Details:"
    gst-inspect-1.0 nv264enc | grep -E "Factory Details|element" | head -2 | sed 's/^/    /'
else
    echo "✗ nv264enc plugin not found"
fi

if gst-inspect-1.0 nvh264enc > /dev/null 2>&1; then
    echo "✓ nvh264enc (NVIDIA H.264) plugin found"
    echo "  Details:"
    gst-inspect-1.0 nvh264enc | grep -E "Factory Details|element" | head -2 | sed 's/^/    /'
else
    echo "✗ nvh264enc plugin not found"
fi

if gst-inspect-1.0 nvh265enc > /dev/null 2>&1; then
    echo "✓ nvh265enc (NVIDIA H.265/HEVC) plugin found"
    echo "  Details:"
    gst-inspect-1.0 nvh265enc | grep -E "Factory Details|element" | head -2 | sed 's/^/    /'
else
    echo "✗ nvh265enc plugin not found"
fi

# Check for VAAPI (Intel/AMD hardware encoder)
echo ""
echo "=== VAAPI (Intel/AMD Hardware Encoder) ==="
if gst-inspect-1.0 vaapih264enc > /dev/null 2>&1; then
    echo "✓ vaapih264enc plugin found"
    echo "  Details:"
    gst-inspect-1.0 vaapih264enc | grep -E "Factory Details|element" | head -2 | sed 's/^/    /'
else
    echo "✗ vaapih264enc plugin not found"
fi

if gst-inspect-1.0 vaapih265enc > /dev/null 2>&1; then
    echo "✓ vaapih265enc plugin found"
    echo "  Details:"
    gst-inspect-1.0 vaapih265enc | grep -E "Factory Details|element" | head -2 | sed 's/^/    /'
else
    echo "✗ vaapih265enc plugin not found"
fi

# Check VAAPI info if available
if command -v vainfo > /dev/null 2>&1; then
    echo ""
    echo "VAAPI hardware capabilities:"
    vainfo 2>/dev/null | grep -E "VAProfile|VAEntrypoint" | head -10 | sed 's/^/  /' || echo "  (run 'vainfo' manually for details)"
fi

# Check for V4L2 hardware encoder (common on embedded platforms)
echo ""
echo "=== V4L2 Hardware Encoder ==="
if gst-inspect-1.0 v4l2h264enc > /dev/null 2>&1; then
    echo "✓ v4l2h264enc plugin found"
    echo "  Details:"
    gst-inspect-1.0 v4l2h264enc | grep -E "Factory Details|element" | head -2 | sed 's/^/    /'
else
    echo "✗ v4l2h264enc plugin not found"
fi

if gst-inspect-1.0 v4l2h265enc > /dev/null 2>&1; then
    echo "✓ v4l2h265enc plugin found"
    echo "  Details:"
    gst-inspect-1.0 v4l2h265enc | grep -E "Factory Details|element" | head -2 | sed 's/^/    /'
else
    echo "✗ v4l2h265enc plugin not found"
fi

# Check for V4L2 devices
if command -v v4l2-ctl > /dev/null 2>&1; then
    echo ""
    echo "V4L2 video devices:"
    v4l2-ctl --list-devices 2>/dev/null | head -20 | sed 's/^/  /' || echo "  (run 'v4l2-ctl --list-devices' manually for details)"
    
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
    echo "  Details:"
    gst-inspect-1.0 omxh264enc | grep -E "Factory Details|element" | head -2 | sed 's/^/    /'
else
    echo "✗ omxh264enc plugin not found"
fi

if gst-inspect-1.0 omxh265enc > /dev/null 2>&1; then
    echo "✓ omxh265enc plugin found"
    echo "  Details:"
    gst-inspect-1.0 omxh265enc | grep -E "Factory Details|element" | head -2 | sed 's/^/    /'
else
    echo "✗ omxh265enc plugin not found"
fi

# List all available encoders
echo ""
echo "=== All Available Video Encoders ==="
ENCODERS=$(gst-inspect-1.0 | grep -i "enc" | grep -E "h264|h265|hevc|264|265" | sort)
if [ -n "$ENCODERS" ]; then
    echo "$ENCODERS" | head -20 | sed 's/^/  /'
    COUNT=$(echo "$ENCODERS" | wc -l)
    if [ "$COUNT" -gt 20 ]; then
        echo "  ... and $((COUNT - 20)) more (run 'gst-inspect-1.0 | grep -i enc' for full list)"
    fi
else
    echo "  No hardware encoders found"
fi

echo ""
echo "=== Summary ==="
HW_FOUND=0
# Check Jetson encoders first
gst-inspect-1.0 nvv4l2h264enc > /dev/null 2>&1 && HW_FOUND=1
gst-inspect-1.0 nvv4l2h265enc > /dev/null 2>&1 && HW_FOUND=1
gst-inspect-1.0 nvv4l2vp9enc > /dev/null 2>&1 && HW_FOUND=1
gst-inspect-1.0 nvv4l2av1enc > /dev/null 2>&1 && HW_FOUND=1
# Check other encoders
gst-inspect-1.0 nv264enc > /dev/null 2>&1 && HW_FOUND=1
gst-inspect-1.0 nvh264enc > /dev/null 2>&1 && HW_FOUND=1
gst-inspect-1.0 nvh265enc > /dev/null 2>&1 && HW_FOUND=1
gst-inspect-1.0 vaapih264enc > /dev/null 2>&1 && HW_FOUND=1
gst-inspect-1.0 vaapih265enc > /dev/null 2>&1 && HW_FOUND=1
gst-inspect-1.0 v4l2h264enc > /dev/null 2>&1 && HW_FOUND=1
gst-inspect-1.0 v4l2h265enc > /dev/null 2>&1 && HW_FOUND=1
gst-inspect-1.0 omxh264enc > /dev/null 2>&1 && HW_FOUND=1
gst-inspect-1.0 omxh265enc > /dev/null 2>&1 && HW_FOUND=1

if [ "$HW_FOUND" -eq 1 ]; then
    echo "✓ Hardware encoder support detected!"
    
    # Check if hardware device is accessible
    if [ -e /dev/nvhost-msenc ]; then
        if [ -r /dev/nvhost-msenc ] && [ -w /dev/nvhost-msenc ]; then
            echo "  ✓ Hardware encoder device is accessible"
            echo "  You can use hardware encoders for better performance."
        else
            echo "  ⚠ Hardware encoder device exists but may have permission issues"
            echo "  Try: sudo chmod 666 /dev/nvhost-msenc"
        fi
    else
        echo "  ⚠ Plugin found but hardware device not accessible"
    fi
else
    echo "✗ No hardware encoder support detected."
    echo "  You'll need to use software encoders (x264enc, vp8enc, etc.)"
fi

echo ""
echo "=== System Information ==="
echo "Kernel: $(uname -r)"
echo "Architecture: $(uname -m)"
if [ -r /proc/device-tree/model ]; then
    echo "Device: $(cat /proc/device-tree/model 2>/dev/null | tr -d '\0')"
fi
if command -v nvidia-smi > /dev/null 2>&1; then
    echo "GPU Info:"
    nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | sed 's/^/  /' || echo "  (nvidia-smi not available or no GPU)"
fi

