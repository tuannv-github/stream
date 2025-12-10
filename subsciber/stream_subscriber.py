#! /usr/bin/env python3

import sys
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
import time
from PyQt5.uic import loadUi
import threading
from enum import Enum
import json
import os
import shutil
from pathlib import Path

import sys
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QOpenGLWidget
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GObject, GstVideo

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "stream_subscriber.conf")
DEFAULT_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "stream_subscriber.default.conf")

# Global URLs variable - will be initialized from config
URLs = []

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
        
        # Print pipeline structure
        print("\n" + "="*80)
        print("GStreamer Pipeline:")
        print("="*80)
        print(f"Pipeline: {self.pipeline.get_name()}")
        elements = []
        it = self.pipeline.iterate_elements()
        while True:
            result, element = it.next()
            if result == Gst.IteratorResult.DONE:
                break
            if result == Gst.IteratorResult.OK:
                elements.append(element.get_name())
        print(f"Elements: {' -> '.join(elements)}")
        print(f"Pipeline description: rtspsrc -> rtph264depay -> h264parse -> avdec_h264 -> videoconvert -> glimagesink")
        print("="*80 + "\n")

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

def load_settings():
    """Load settings from configuration file."""
    # Copy default config if config file doesn't exist
    if not os.path.exists(CONFIG_FILE) and os.path.exists(DEFAULT_CONFIG_FILE):
        try:
            shutil.copy2(DEFAULT_CONFIG_FILE, CONFIG_FILE)
            print(f"Created {CONFIG_FILE} from {DEFAULT_CONFIG_FILE}")
        except IOError as e:
            print(f"Error copying default config file: {e}")
    
    # Load defaults from default config file
    default_settings = None
    if os.path.exists(DEFAULT_CONFIG_FILE):
        try:
            with open(DEFAULT_CONFIG_FILE, 'r') as f:
                default_settings = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error loading default config file: {e}")
    
    # Fallback defaults if default config file doesn't exist or is invalid
    if default_settings is None:
        default_settings = {
            "urls": [],
            "url_index": 0,
            "window_x": None,
            "window_y": None,
            "window_width": None,
            "window_height": None
        }
    
    # Load actual config file
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                settings = json.load(f)
                # Merge with defaults to ensure all keys exist
                for key in default_settings:
                    if key not in settings:
                        settings[key] = default_settings[key]
                # Validate URLs structure
                if "urls" in settings and isinstance(settings["urls"], list):
                    # Ensure each URL has required fields
                    valid_urls = []
                    for url_item in settings["urls"]:
                        if isinstance(url_item, dict) and "url" in url_item and "name" in url_item:
                            valid_urls.append(url_item)
                    if valid_urls:
                        settings["urls"] = valid_urls
                    else:
                        settings["urls"] = default_settings.get("urls", [])
                else:
                    settings["urls"] = default_settings.get("urls", [])
                return settings
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error loading config file: {e}. Using defaults.")
            return default_settings
    
    return default_settings

def save_settings(settings):
    """Save settings to configuration file."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(settings, f, indent=4)
    except IOError as e:
        print(f"Error saving config file: {e}")

class Open(QWidget):
    def __init__(self, initial_url_index=0, urls_list=None):
        super().__init__()
        loadUi("ui/Open.ui", self)
        
        if urls_list is None:
            urls_list = URLs

        # palette = self.palette()
        # palette.setColor(QPalette.Window, QColor(0, 255, 0))  # RGB color
        # self.setPalette(palette)
        # self.setAutoFillBackground(True)  # Required for palette to take effect

        comboBox_URL = self.findChild(QComboBox, "comboBox_URL")
        comboBox_URL.addItems([url["name"] for url in urls_list])
        comboBox_URL.setCurrentIndex(initial_url_index)
        comboBox_URL.currentIndexChanged.connect(self.on_url_changed)
    
    def on_url_changed(self, index):
        """Save URL index when changed."""
        settings = load_settings()
        settings["url_index"] = index
        save_settings(settings)
        

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
    def __init__(self, initial_url_index=0, urls_list=None):
        super().__init__()
        loadUi("ui/Player.ui", self)
        
        if urls_list is None:
            urls_list = URLs
        self.urls_list = urls_list

        # palette = self.palette()
        # palette.setColor(QPalette.Window, QColor(100, 150, 200))  # RGB color
        # self.setPalette(palette)
        # self.setAutoFillBackground(True)  # Required for palette to take effect

        layout = QVBoxLayout()
        self.setLayout(layout)

        self.widgetOpen = Open(initial_url_index, urls_list)
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
            url = self.urls_list[index]
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

        # Load settings
        settings = load_settings()
        url_index = settings.get("url_index", 0)
        
        # Use global URLs (already loaded in main)
        self.player0 = Player(url_index, URLs)
        self.layout().addWidget(self.player0)

        # self.player1 = Player()
        # self.layout().addWidget(self.player1)

        self.statusBar().setStyleSheet("background-color: white;")
        self.show_status_bar("Ready")
        
        # Apply saved window geometry or use defaults
        saved_x = settings.get("window_x")
        saved_y = settings.get("window_y")
        saved_width = settings.get("window_width")
        saved_height = settings.get("window_height")
        
        if (saved_x is not None and saved_y is not None and 
            saved_width is not None and saved_height is not None):
            # Validate that the saved geometry is on a valid screen
            screen_geometry = QDesktopWidget().availableGeometry()
            if (0 <= saved_x < screen_geometry.width() and 
                0 <= saved_y < screen_geometry.height() and
                saved_width > 0 and saved_height > 0):
                self.setGeometry(saved_x, saved_y, saved_width, saved_height)
            else:
                # Use default centered geometry if saved geometry is invalid
                self._set_default_geometry()
        else:
            # Use default centered geometry if no saved geometry
            self._set_default_geometry()
    
    def _set_default_geometry(self):
        """Set default centered window geometry."""
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
    
    def closeEvent(self, event):
        """Save settings when window is closed."""
        settings = load_settings()
        geometry = self.geometry()
        settings["window_x"] = geometry.x()
        settings["window_y"] = geometry.y()
        settings["window_width"] = geometry.width()
        settings["window_height"] = geometry.height()
        # Save current URLs
        settings["urls"] = URLs
        save_settings(settings)
        event.accept()

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
    
    # Load settings and initialize global URLs
    # load_settings() will automatically copy from default config if needed
    settings = load_settings()
    # Update the global URLs list
    URLs.clear()
    URLs.extend(settings.get("urls", []))
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
