import sys
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QOpenGLWidget
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GObject, GstVideo

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

        # Create a GStreamer pipeline
        self.pipeline = Gst.parse_launch("playbin uri=file:///home/fcp/fhd.mp4")

        # Create the video sink using glimagesink
        video_sink = Gst.ElementFactory.make("glimagesink", "video_sink")
        
        # Set the OpenGLWidget window ID for rendering (this uses the correct method)
        video_sink.set_property("force-aspect-ratio", True)
        video_sink.set_window_handle(self.gstreamer_window_id)
        
        # Set the video sink in the pipeline
        self.pipeline.set_property("video-sink", video_sink)

    def play_video(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        print("Video started...")

    def closeEvent(self, event):
        # Stop the pipeline when the window is closed
        self.pipeline.set_state(Gst.State.NULL)
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    
    window = MediaPlayer()
    window.show()
    
    sys.exit(app.exec_())
