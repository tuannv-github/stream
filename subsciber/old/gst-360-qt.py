import sys
from PyQt5.QtCore import Qt, QThread
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QOpenGLWidget
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GObject, GstVideo

class GStreamerThread(QThread):
    def __init__(self, pipeline):
        super().__init__()
        self.pipeline = pipeline

    def run(self):
        bus = self.pipeline.get_bus()
        while True:
            message = bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            
            if message.type == Gst.MessageType.EOS:
                print("End of stream reached.")
                break
            elif message.type == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                print(f"Error occurred: {err}, {debug}")
                break

class MediaPlayer(QWidget):
    def __init__(self):
        super().__init__()

        Gst.init(None)  # Initialize GStreamer
        
        self.setWindowTitle("GStreamer Media Player")
        self.setGeometry(100, 100, 640, 480)
        
        self.layout = QVBoxLayout()

        # Button to start video playback
        self.play_button = QPushButton("Play Video", self)
        self.layout.addWidget(self.play_button)
        self.play_button.clicked.connect(self.play_video)
        
        self.setLayout(self.layout)

        # Create a QOpenGLWidget for video rendering
        self.opengl_widget = QOpenGLWidget(self)
        self.opengl_widget.setGeometry(0, 50, 640, 360)  # Position the video inside the window
        self.layout.addWidget(self.opengl_widget)

        # Get the window ID
        self.gstreamer_window_id = self.opengl_widget.winId()

        self.pipeline = Gst.Pipeline.new("rtsp-pipeline")

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

        sink.set_property("force-aspect-ratio", True)
        sink.set_window_handle(self.gstreamer_window_id)

        # Add elements to the pipeline
        self.pipeline.add(source)
        self.pipeline.add(rtph264depay)
        self.pipeline.add(h264parse)
        self.pipeline.add(decoder)
        self.pipeline.add(convert)
        self.pipeline.add(sink)

        # Link static elements
        rtph264depay.link(h264parse)
        h264parse.link(decoder)
        decoder.link(convert)
        convert.link(sink)

        def on_pad_added(element, pad):
            caps = pad.query_caps(None)
            name = caps.to_string()
            if name.startswith("application/x-rtp"):
                sink_pad = rtph264depay.get_static_pad("sink")
                pad.link(sink_pad)
        source.connect("pad-added", on_pad_added)

        self.gstreamer_thread = GStreamerThread(self.pipeline)  # Create the thread for bus message handling

    def play_video(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        print("Video started...")

        self.gstreamer_thread.start()  # Start the thread to handle bus messages

    def closeEvent(self, event):
        # Stop the pipeline when the window is closed
        self.pipeline.set_state(Gst.State.NULL)
        self.gstreamer_thread.quit()  # Stop the bus message thread
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    
    window = MediaPlayer()
    window.show()
    
    sys.exit(app.exec_())
