import sys
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
import time
from PyQt5.uic import loadUi
import threading
from enum import Enum

import sys
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QOpenGLWidget
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GObject, GstVideo

URLs = [
    {
        "url": "rtsp://10.1.101.210:8554/live/stream/kobuki",
        "name": "Stream Kuboki",
    },
]

FONT_SIZE_PIXELS = 0

class VideoState(Enum):
    STATE_CLOSE = 0
    STATE_CONNECTING = 1
    STATE_OPEN = 2

class Video(QOpenGLWidget):
    
    sig_state_changed = pyqtSignal(VideoState)

    def __change_state(self, state):
        self.state = state
        if self.state == VideoState.STATE_CLOSE:
            print(f"Video state changed to CLOSE")
        elif self.state == VideoState.STATE_OPEN:
            print(f"Video state changed to OPEN")
        elif self.state == VideoState.STATE_CONNECTING:
            print(f"Video state changed to CONNECTING")
        self.sig_state_changed.emit(self.state)

    def __create_pipeline(self):
        self.pipeline = Gst.Pipeline.new("rtsp-pipeline")

        self.source = Gst.ElementFactory.make("rtspsrc", "source")
        rtph264depay = Gst.ElementFactory.make("rtph264depay", "depay")
        h264parse = Gst.ElementFactory.make("h264parse", "parser")
        decoder = Gst.ElementFactory.make("avdec_h264", "decoder")
        convert = Gst.ElementFactory.make("videoconvert", "convert")
        sink = Gst.ElementFactory.make("glimagesink", "sink")

        if not all([self.source, rtph264depay, h264parse, decoder, convert, sink]):
            print("Failed to create elements")

        self.source.set_property("latency", 100)  # Adjust latency for real-time streaming
        # self.source.set_property("tcp-timeout", 2000000)
        # self.source.set_property("timeout", 2000000)

        sink.set_property("force-aspect-ratio", True)
        sink.set_property("sync", False)
        sink.set_window_handle(self.winId())

        self.pipeline.add(self.source)
        self.pipeline.add(rtph264depay)
        self.pipeline.add(h264parse)
        self.pipeline.add(decoder)
        self.pipeline.add(convert)
        self.pipeline.add(sink)

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
        self.source.connect("pad-added", on_pad_added)

    def open_stream(self, URL_index, max_tries=None):
        if self.state == VideoState.STATE_OPEN:
            print("Video is already open")

        print("Opening stream... at URL:", URLs[URL_index]["url"])
        self.source.set_property("location", URLs[URL_index]["url"])
        self.pipeline.set_state(Gst.State.PLAYING)
        self.__change_state(VideoState.STATE_CONNECTING)

    def close_stream(self):
        if self.state == VideoState.STATE_CLOSE:
            print("Video is already closed")
            return
        elif self.state == VideoState.STATE_OPEN or self.state == VideoState.STATE_CONNECTING:
            print("Closing stream...")
            self.pipeline.set_state(Gst.State.NULL)
            self.__change_state(VideoState.STATE_CLOSE)

    def __init__(self):
        super().__init__()

        # palette = self.palette()
        # palette.setColor(QPalette.Window, QColor(255, 0, 0))  # RGB color
        # self.setPalette(palette)
        # self.setAutoFillBackground(True)

        Gst.init(None)  # Initialize GStreamer

        self.__create_pipeline()
        self.__change_state(VideoState.STATE_CLOSE)

        self.bus_thread = threading.Thread(target=self.pipeline_bus_check)
        self.bus_thread.daemon = True  # Allow main application to exit even if thread is still running
        self.bus_thread.start()

    def pipeline_bus_check(self):
        bus = self.pipeline.get_bus()
        timeout_counter = 0
        reconnecting_counter = 0
        while True:
            msg = bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING | Gst.MessageType.STATE_CHANGED)
            if msg is None:
                continue
            if self.state == VideoState.STATE_CLOSE or self.state == VideoState.STATE_OPEN:
                timeout_counter = 0
            msg_type = msg.type
            if msg_type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                print(f"❌ Error: {err}, Debug: {debug}")
                if (self.state == VideoState.STATE_OPEN):
                    reconnecting_counter += 1
                    print("Reconnecting... " + str(reconnecting_counter))
                    self.pipeline.set_state(Gst.State.NULL)
                    self.pipeline.set_state(Gst.State.PLAYING)
                    time.sleep(1)
                elif (self.state == VideoState.STATE_CONNECTING):
                    timeout_counter += 1
                    reconnecting_counter += 1
                    print("Reconnecting... " + str(reconnecting_counter))
                    self.pipeline.set_state(Gst.State.NULL)
                    self.pipeline.set_state(Gst.State.PLAYING)
                    print(f"Timeout counter: {timeout_counter} over 10")
                    if timeout_counter > 10:
                        print("Timeout reached, closing stream...")
                        self.pipeline.set_state(Gst.State.NULL)
                        self.__change_state(VideoState.STATE_CLOSE)
                    time.sleep(1)
            elif msg_type == Gst.MessageType.EOS:
                print("✅ End of Stream reached")
                if (self.state == VideoState.STATE_OPEN):
                    reconnecting_counter += 1
                    print("Reconnecting... " + str(reconnecting_counter))
                    self.pipeline.set_state(Gst.State.NULL)
                    self.pipeline.set_state(Gst.State.PLAYING)
                    time.sleep(1)
                else:
                    self.__change_state(VideoState.STATE_CLOSE)
            elif msg_type == Gst.MessageType.WARNING:
                warn, debug = msg.parse_warning()
                print(f"⚠️ Warning: {warn}, Debug: {debug}")
                if "Could not read from resource." in str(warn):
                    if (self.state == VideoState.STATE_OPEN):
                        reconnecting_counter += 1
                        print("Reconnecting... " + str(reconnecting_counter))
                        self.pipeline.set_state(Gst.State.NULL)
                        self.pipeline.set_state(Gst.State.PLAYING)
                        time.sleep(1)
            elif msg_type == Gst.MessageType.STATE_CHANGED:
                old_state, new_state, pending = msg.parse_state_changed()
                src = msg.src  # The element that changed state
                # if isinstance(src, Gst.Element):  # Ensure it's a GStreamer element
                #     print(f"🔄 {src.name}: {old_state} → {new_state} (Pending: {pending})")
                if src.name == "rtsp-pipeline" and new_state == Gst.State.PLAYING:
                    timeout_counter = 0
                    reconnecting_counter = 0
                    self.__change_state(VideoState.STATE_OPEN)
            else:
                print(f"📢 Other Message: {msg_type}")

    def resizeEvent(self, event):
        print(f"Video resized to: {event.size().width()}x{event.size().height()}")

    def closeEvent(self, event):
        self.close_stream()

class Open(QWidget):
    def __init__(self):
        super().__init__()
        loadUi("ui/Open.ui", self)

        # palette = self.palette()
        # palette.setColor(QPalette.Window, QColor(0, 255, 0))  # RGB color
        # self.setPalette(palette)
        # self.setAutoFillBackground(True)  # Required for palette to take effect

        comboBox_URL = self.findChild(QComboBox, "comboBox_URL")
        comboBox_URL.addItems([url["name"] for url in URLs])
        comboBox_URL.setCurrentIndex(0)
        

    def resizeEvent(self, event):
        print(f"Open resized to: {event.size().width()}x{event.size().height()}")
        comboBox_URL = self.findChild(QComboBox, "comboBox_URL")
        pushButton_Open = self.findChild(QPushButton, "pushButton_Open")
        line_Open = self.findChild(QFrame, "line_Open")
        pushButton_Open.setGeometry(FONT_SIZE_PIXELS, int(event.size().height()/2 - FONT_SIZE_PIXELS*1.2), FONT_SIZE_PIXELS * 10, FONT_SIZE_PIXELS * 2)
        comboBox_URL.setGeometry(pushButton_Open.x() + pushButton_Open.width() + FONT_SIZE_PIXELS, int(event.size().height()/2 - FONT_SIZE_PIXELS*1.2), event.size().width() - pushButton_Open.width() - FONT_SIZE_PIXELS*3, FONT_SIZE_PIXELS * 2)
        line_Open.setGeometry(0, int(event.size().height() - FONT_SIZE_PIXELS), event.size().width(), line_Open.height())

    def sig_state_changed(self, state):
        comboBox_URL = self.findChild(QComboBox, "comboBox_URL")
        pushButton_Open = self.findChild(QPushButton, "pushButton_Open")
        if state == VideoState.STATE_OPEN:
            comboBox_URL.setEnabled(False)
            pushButton_Open.setEnabled(True)
            pushButton_Open.setText("Close")
        elif state == VideoState.STATE_CLOSE:
            comboBox_URL.setEnabled(True)
            pushButton_Open.setEnabled(True)
            pushButton_Open.setText("Open")
        elif state == VideoState.STATE_CONNECTING:
            comboBox_URL.setEnabled(False)
            pushButton_Open.setEnabled(True)
            pushButton_Open.setText("Connecting...")

class Player(QWidget):
    def __init__(self):
        super().__init__()
        loadUi("ui/Player.ui", self)

        # palette = self.palette()
        # palette.setColor(QPalette.Window, QColor(100, 150, 200))  # RGB color
        # self.setPalette(palette)
        # self.setAutoFillBackground(True)  # Required for palette to take effect

        layout = QVBoxLayout()
        self.setLayout(layout)

        self.widgetOpen = Open()
        self.layout().addWidget(self.widgetOpen)

        self.widgetVideo = Video()
        self.layout().addWidget(self.widgetVideo)

        pushButton_Open = self.widgetOpen.findChild(QPushButton, "pushButton_Open")
        pushButton_Open.clicked.connect(self.on_open_button_clicked)
        pushButton_Open.setEnabled(True)
        
        self.widgetVideo.sig_state_changed.connect(self.widgetOpen.sig_state_changed)

    def on_open_button_clicked(self):
        if self.widgetVideo.state == VideoState.STATE_OPEN or self.widgetVideo.state == VideoState.STATE_CONNECTING:
            self.widgetVideo.close_stream()
        else:
            comboBox_URL = self.widgetOpen.findChild(QComboBox, "comboBox_URL")
            index = comboBox_URL.currentIndex()
            url = URLs[index]
            self.widgetVideo.open_stream(index)

    def resizeEvent(self, event):
        print(f"Player resized to: {event.size().width()}x{event.size().height()}")

        self.widgetOpen.setGeometry(0, 0, self.width(), FONT_SIZE_PIXELS * 4)
        self.frame_player.setGeometry(0, FONT_SIZE_PIXELS * 4, event.size().width(), event.size().height() - self.widgetOpen.height() - int(FONT_SIZE_PIXELS + FONT_SIZE_PIXELS/2))
        self.widgetVideo.setGeometry(0, FONT_SIZE_PIXELS * 4, event.size().width(), event.size().height() - self.widgetOpen.height() - int(FONT_SIZE_PIXELS + FONT_SIZE_PIXELS/2))

        super().resizeEvent(event)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        loadUi("ui/Live.ui", self)

        self.player0 = Player()
        self.layout().addWidget(self.player0)

        # self.player1 = Player()
        # self.layout().addWidget(self.player1)

        self.statusBar().setStyleSheet("background-color: white;")
        self.show_status_bar("Ready")
        
        screen_geometry = QDesktopWidget().availableGeometry()
        screen_center_x = screen_geometry.width() // 2
        screen_center_y = screen_geometry.height() // 2
        window_width = int(1280 * FONT_SIZE_PIXELS / 20)
        window_height = int(720 * FONT_SIZE_PIXELS / 20)
        self.setGeometry(
            screen_center_x - window_width // 2,
            screen_center_y - window_height // 2,
            window_width,
            window_height
        )

    def resizeEvent(self, event):
        print(f"MainWindow resized to: {event.size().width()}x{event.size().height()}")
        self.player0.setGeometry(0, 0, int(event.size().width()), int(event.size().height()))
        # self.player0.setGeometry(0, 0, int(event.size().width()), int(event.size().height()/2))
        # self.player1.setGeometry(0, int(event.size().height()/2), int(event.size().width()), int(event.size().height()/2))
        super().resizeEvent(event)
    
    def show_status_bar(self, message, timeout_miliseconds=None):
        self.statusBar().showMessage(message)
        if timeout_miliseconds is None:
            return
        QTimer.singleShot(timeout_miliseconds, lambda: self.statusBar().clearMessage())

if __name__ == '__main__':
    app = QApplication(sys.argv)

    FONT_SIZE_PIXELS = int(QWidget().font().pointSize() * app.primaryScreen().logicalDotsPerInch() / 72.0)
    print(f"FONT_SIZE_PIXELS: {FONT_SIZE_PIXELS}")
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
