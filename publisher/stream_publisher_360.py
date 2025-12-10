import gi
import time
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib, GObject

# Initialize GStreamer
Gst.init(None)

# Create pipeline
pipeline_str = """
thetauvcsrc mode=1 ! decodebin ! nvvidconv qos=true flip-method=2 ! 
textoverlay name=overlay text="Initializing..." valignment=top halignment=left font-desc="Sans, 24" ! 
nvv4l2h264enc bitrate=25000000 iframeinterval=30 idrinterval=1 maxperf-enable=true ! 
h264parse ! flvmux name=mux streamable=false ! 
rtmpsink location='rtmp://10.10.0.11:1935/live/stream/360' sync=false async=false qos=true
"""

pipeline = Gst.parse_launch(pipeline_str)

# Get the textoverlay element
overlay = pipeline.get_by_name("overlay")

# Function to update the overlay text in real-time
def update_overlay():
    while True:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}"
        overlay.set_property("text", f"Live {timestamp}")
        time.sleep(0.05)  # Update every 50ms

# Start pipeline
pipeline.set_state(Gst.State.PLAYING)

# Run the overlay update in a separate thread
import threading
overlay_thread = threading.Thread(target=update_overlay, daemon=True)
overlay_thread.start()

# Run GStreamer loop
loop = GLib.MainLoop()
try:
    loop.run()
except KeyboardInterrupt:
    print("Stopping...")
    pipeline.set_state(Gst.State.NULL)
