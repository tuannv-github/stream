#!/bin/bash

# Configuration
DISPLAY_ID=":1"
XAUTH="/run/user/1000/gdm/Xauthority"
OUTPUT="HDMI-0"
MODE="1920x1080"
RATE="60.00"

echo "Attempting to force output signal to $OUTPUT..."

# Export environment variables for X11 interaction
export DISPLAY=$DISPLAY_ID
export XAUTHORITY=$XAUTH

# Check if Output exists
if xrandr --query | grep -q "$OUTPUT connected"; then
    echo "Found $OUTPUT connected. Applying settings..."
    
    # 1. Ensure the output is enabled and set to the desired resolution
    xrandr --output "$OUTPUT" --mode "$MODE" --rate "$RATE" --primary --auto
    
    # 2. Re-verify
    sleep 1
    CURRENT_RES=$(xrandr --query | grep "$OUTPUT connected" | awk '{print $3}')
    echo "Current resolution on $OUTPUT: $CURRENT_RES"
    
    echo "Done. If you still see no signal, try unplugging and re-plugging the HDMI cable."
else
    echo "Error: $OUTPUT is not detected as connected by xrandr."
    echo "Available outputs:"
    xrandr --query | grep " connected"
fi
