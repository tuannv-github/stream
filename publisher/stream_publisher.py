import gi
import time
from gi.repository import Gst, GLib

gi.require_version('Gst', '1.0')

# Initialize GStreamer
Gst.init(None)

# Create the GStreamer pipeline
pipeline = Gst.parse_launch('v4l2src device=/dev/video0 ! video/x-raw,format=YUY2,width=640,height=480,framerate=30/1 ! videoconvert ! '
                            'textoverlay name=overlay halignment=left valignment=top font-desc="Sans, 24" shaded-background=true ! '
                            'x264enc tune=zerolatency bitrate=2500 speed-preset=superfast ! flvmux ! '
                            'rtmpsink location="rtmp://10.10.0.11/live/stream/hd" sync=false')

# Access the textoverlay element
overlay = pipeline.get_by_name('overlay')

# Start the pipeline
pipeline.set_state(Gst.State.PLAYING)

# Function to update the overlay text with current time and milliseconds
def update_overlay():
    # Get current time with milliseconds
    current_time = time.strftime("%Y-%m-%d %H:%M:%S.") + str(int(time.time() * 1000) % 1000).zfill(3) + " in SG"
    overlay.set_property('text', current_time)
    return True  # Keep updating every 100 milliseconds

# Create a GLib main loop and run the update every 100 milliseconds
loop = GLib.MainLoop()
GLib.timeout_add(100, update_overlay)  # Update every 100 milliseconds

try:
    loop.run()
except KeyboardInterrupt:
    pipeline.set_state(Gst.State.NULL)
