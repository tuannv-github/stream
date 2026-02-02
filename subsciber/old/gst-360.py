import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GObject

# Initialize GStreamer
Gst.init(None)

# Create the pipeline
pipeline = Gst.Pipeline.new("rtsp-pipeline")

# Create elements
source = Gst.ElementFactory.make("rtspsrc", "source")
rtph264depay = Gst.ElementFactory.make("rtph264depay", "depay")
h264parse = Gst.ElementFactory.make("h264parse", "parser")
decoder = Gst.ElementFactory.make("avdec_h264", "decoder")
convert = Gst.ElementFactory.make("videoconvert", "convert")
sink = Gst.ElementFactory.make("glimagesink", "sink")

# Check if elements are created
if not all([source, rtph264depay, h264parse, decoder, convert, sink]):
    print("Failed to create elements")
    exit(1)

# Set RTSP source location
source.set_property("location", "rtsp://10.10.0.11:8554/live/stream/360")
source.set_property("latency", 100)  # Adjust latency for real-time streaming

# Add elements to the pipeline
pipeline.add(source)
pipeline.add(rtph264depay)
pipeline.add(h264parse)
pipeline.add(decoder)
pipeline.add(convert)
pipeline.add(sink)

# Link static elements
rtph264depay.link(h264parse)
h264parse.link(decoder)
decoder.link(convert)
convert.link(sink)

# Handle dynamic linking for rtspsrc
def on_pad_added(element, pad):
    caps = pad.query_caps(None)
    name = caps.to_string()
    if name.startswith("application/x-rtp"):
        sink_pad = rtph264depay.get_static_pad("sink")
        pad.link(sink_pad)

source.connect("pad-added", on_pad_added)

# Run the pipeline
pipeline.set_state(Gst.State.PLAYING)

# Capture all messages from the bus
bus = pipeline.get_bus()

while True:
    msg = bus.timed_pop(Gst.CLOCK_TIME_NONE)  # Get all messages
    if msg:
        msg_type = msg.type
        if msg_type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            print(f"❌ Error: {err}, Debug: {debug}")
            break
        elif msg_type == Gst.MessageType.EOS:
            print("✅ End of Stream reached")
            break
        elif msg_type == Gst.MessageType.WARNING:
            warn, debug = msg.parse_warning()
            print(f"⚠️ Warning: {warn}, Debug: {debug}")
        elif msg_type == Gst.MessageType.STATE_CHANGED:
            # Get the element changing state
            src = msg.src  # The element that changed state
            old_state, new_state, pending = msg.parse_state_changed()
            if isinstance(src, Gst.Element):  # Ensure it's a GStreamer element
                print(f"🔄 {src.name}: {old_state} → {new_state} (Pending: {pending})")
        # elif msg_type == Gst.MessageType.ELEMENT:
        #     struct = msg.get_structure()
        #     if struct:
        #         print(f"📡 Element Message: {struct.to_string()}")
        # else:
        #     print(f"📢 Other Message: {msg_type}")

# Cleanup
pipeline.set_state(Gst.State.NULL)
