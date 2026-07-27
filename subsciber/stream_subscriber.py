#! /usr/bin/env python3

import sys
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
import time
from PyQt5.uic import loadUi
import threading
from enum import Enum
import os
import yaml
import shutil
from pathlib import Path
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

import sys
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GObject, GstVideo

# InfluxDB client
try:
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
    INFLUXDB_AVAILABLE = True
except ImportError:
    INFLUXDB_AVAILABLE = False
    logger_temp = logging.getLogger(__name__)
    logger_temp.warning("influxdb-client not installed. Install with: pip install influxdb-client")

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "stream_subscriber.yaml")
DEFAULT_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "stream_subscriber.default.yaml")
LOG_FILE = os.path.join(os.path.dirname(__file__), "stream_subscriber.log")

# Single source of default settings (used when config file missing or invalid)
DEFAULT_SETTINGS = {
    "urls": [],
    "url_index": 0,
    "window_x": None,
    "window_y": None,
    "window_width": None,
    "window_height": None,
    "rtsp_transport": "tcp",
    "influxdb_url": "http://localhost:8086",
    "influxdb_org": "fcclab",
    "influxdb_bucket": "fcclab",
    "influxdb_token": "fcclab_token",
    "influxdb_measurement": "stream_metrics",
    # auto: pick best available; under Wayland use qt_use_xcb_under_wayland so embed works
    "video_sink": "auto",
    "qt_use_xcb_under_wayland": True,
}

# Configure logging
def setup_logging(log_file=None, log_level=logging.DEBUG):
    """Setup logging configuration with file and console handlers."""
    if log_file is None:
        log_file = LOG_FILE
    
    # Create logs directory if it doesn't exist
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    
    # Create formatters
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_formatter = logging.Formatter(
        '%(levelname)s - %(message)s'
    )
    
    # Setup root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()
    
    # File handler with rotation (10MB max, keep 5 backup files)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    return root_logger

# Initialize logging
logger = setup_logging()


def to_playable_url(url):
    """
    Convert a public subscribe URL to a GStreamer-playable location.

    Public form:  http://10.1.106.210/
    Playable:     rtsp://10.1.106.210:8554/stream
    """
    if not url:
        return url
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in ("http", "https"):
        host = parsed.hostname or "10.1.106.210"
        path = parsed.path if parsed.path and parsed.path != "/" else "/stream"
        if not path.startswith("/"):
            path = f"/{path}"
        playable = f"rtsp://{host}:8554{path}"
        logger.info(f"Mapped public URL {url} -> {playable}")
        return playable
    return url


def _bootstrap_qt_for_gstreamer_embed():
    """GStreamer VideoOverlay + Qt winId() is unreliable on native Wayland; use Xcb (XWayland) by default."""
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    if os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland":
        return
    enabled = DEFAULT_SETTINGS.get("qt_use_xcb_under_wayland", True)
    cfg = None
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as fh:
                cfg = yaml.safe_load(fh)
        elif os.path.exists(DEFAULT_CONFIG_FILE):
            with open(DEFAULT_CONFIG_FILE, "r") as fh:
                cfg = yaml.safe_load(fh)
    except Exception:
        cfg = None
    if cfg and "qt_use_xcb_under_wayland" in cfg:
        enabled = bool(cfg["qt_use_xcb_under_wayland"])
    if not enabled:
        return
    os.environ["QT_QPA_PLATFORM"] = "xcb"
    logger.info(
        "Wayland session: using QT_QPA_PLATFORM=xcb so GStreamer can embed into the Qt window "
        "(set qt_use_xcb_under_wayland: false in stream_subscriber.yaml to use native Wayland Qt instead)")


def _display_sink_candidates(settings):
    """Ordered list of element factory names to try for embedded video."""
    pref = (settings.get("video_sink") or "auto").strip().lower()
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    fallbacks = ["xvimagesink", "ximagesink", "glimagesink", "autovideosink"]
    if pref == "auto":
        if session == "wayland":
            # With QT_QPA_PLATFORM=xcb, X11-style sinks work; still prefer non-GL first
            return ["xvimagesink", "ximagesink", "glimagesink", "autovideosink"]
        return ["xvimagesink", "ximagesink", "glimagesink", "autovideosink"]
    if pref in fallbacks:
        return [pref] + [n for n in fallbacks if n != pref]
    return [pref] + fallbacks


def _make_display_sink(settings, win_id_init=0):
    """Create the best available videosink; returns (element_or_none, factory_name)."""
    last = []
    for factory in _display_sink_candidates(settings):
        el = Gst.ElementFactory.make(factory, "sink")
        if el is None:
            last.append(f"{factory}: missing")
            continue
        try:
            el.set_property("sync", False)
        except Exception:
            pass
        for prop, val in (("force-aspect-ratio", False),):
            try:
                el.set_property(prop, val)
            except Exception:
                pass
        try:
            GstVideo.VideoOverlay.set_window_handle(el, int(win_id_init))
            if win_id_init:
                GstVideo.VideoOverlay.expose(el)
        except Exception:
            try:
                el.set_window_handle(int(win_id_init))
            except Exception as e:
                last.append(f"{factory}: handle init {e}")
        logger.info(f"Display sink: {factory}")
        return el, factory
    logger.error("No display sink could be created. Tried: %s", "; ".join(last) or "none")
    return None, ""


# Global URLs variable - will be initialized from config
URLs = []

FONT_SIZE_PIXELS = 0

# FPS calculation constants
FPS_DURATION = 0.5  # Calculate FPS every 0.5 seconds
METRICS_UPDATE_PERIOD = 0.5 # Calculate metrics every 0.5 seconds
FPS_MOVING_AVERAGE_WINDOW = 10

class VideoState(Enum):
    STATE_CLOSE = 0
    STATE_CONNECTING = 1
    STATE_OPEN = 2

class Video(QWidget):
    
    sig_state_changed = pyqtSignal(VideoState)
    sig_recording_changed = pyqtSignal(str)  # Signal for recording state changes: "recording", "saving", "stopped"
    sig_auto_start_recording = pyqtSignal()  # Signal to auto-start recording after reconnect
    sig_metrics_update = pyqtSignal(float, float) # Signal for metrics update (fps, bitrate)
    # Marshals reconnect scheduling to the GUI thread (required for glimagesink / OpenGL stability)
    sig_schedule_pipeline_reconnect = pyqtSignal(int)

    def __change_state(self, state):
        current_state = getattr(self, 'state', None)
        logger.debug(f"__change_state called: current={current_state}, new={state}")
        self.state = state
        if self.state == VideoState.STATE_CLOSE:
            logger.info("Video state changed to CLOSE")
        elif self.state == VideoState.STATE_OPEN:
            logger.info("Video state changed to OPEN")
            # Auto-start recording if it was accepted before disconnect
            if self.auto_record_on_reconnect:
                logger.info(f"Auto-starting recording after reconnect... (flag={self.auto_record_on_reconnect})")
                # Emit signal to auto-start recording (signals are thread-safe and will be handled in main thread)
                self.sig_auto_start_recording.emit()
                logger.debug("Emitted sig_auto_start_recording signal")
            else:
                logger.debug(f"Not auto-starting: auto_record_on_reconnect={self.auto_record_on_reconnect}")
        elif self.state == VideoState.STATE_CONNECTING:
            logger.info("Video state changed to CONNECTING")
        self.sig_state_changed.emit(self.state)

    def __create_pipeline(self):
        logger.debug("Creating GStreamer pipeline")
        self.pipeline = Gst.Pipeline.new("rtsp-pipeline")

        self.source = Gst.ElementFactory.make("rtspsrc", "source")
        rtph264depay = Gst.ElementFactory.make("rtph264depay", "depay")
        h264parse = Gst.ElementFactory.make("h264parse", "parser")
        
        # Add tee element to split stream for recording
        self.tee = Gst.ElementFactory.make("tee", "tee")
        
        decoder = Gst.ElementFactory.make("avdec_h264", "decoder")
        convert = Gst.ElementFactory.make("videoconvert", "convert")
        scale = Gst.ElementFactory.make("videoscale", "scale")
        self._scale_capsfilter = Gst.ElementFactory.make("capsfilter", "scale_caps")
        settings_vp = load_settings()
        sink, sink_factory = _make_display_sink(settings_vp, int(self.winId()))
        self._video_sink = sink
        self._video_sink_factory = sink_factory or "unknown"

        if not all([self.source, rtph264depay, h264parse, self.tee, decoder, convert, scale, self._scale_capsfilter, sink]):
            logger.error("Failed to create GStreamer elements")
            missing = []
            if not self.source: missing.append("rtspsrc")
            if not rtph264depay: missing.append("rtph264depay")
            if not h264parse: missing.append("h264parse")
            if not self.tee: missing.append("tee")
            if not decoder: missing.append("avdec_h264")
            if not convert: missing.append("videoconvert")
            if not scale: missing.append("videoscale")
            if not self._scale_capsfilter: missing.append("capsfilter")
            if not sink: missing.append(f"videosink ({', '.join(_display_sink_candidates(settings_vp))})")
            logger.error(f"Missing elements: {', '.join(missing)}")
        else:
            logger.debug("All GStreamer elements created successfully")

        if sink is None:
            raise RuntimeError(
                "No video display sink — install GStreamer plugins (e.g. gstreamer1.0-plugins-good, "
                "gstreamer1.0-plugins-base) or set video_sink in stream_subscriber.yaml.")

        self.source.set_property("latency", 100)  # Adjust latency for real-time streaming
        logger.debug("Set rtspsrc latency to 100ms")
        
        # Configure RTSP transport (force TCP by default to avoid UDP timeouts)
        try:
            transport = settings_vp.get("rtsp_transport", "tcp")
            if transport == "tcp":
                self.source.set_property("protocols", 4)  # 4 = GstRTSPLowerTrans.TCP
                logger.info("Forcing RTSP transport to TCP")
            elif transport == "udp":
                self.source.set_property("protocols", 1)  # 1 = GstRTSPLowerTrans.UDP
                logger.info("Forcing RTSP transport to UDP")
            else:
                logger.debug(f"Using default RTSP transport (protocols={transport})")
        except Exception as e:
            logger.error(f"Error setting RTSP transport: {e}")
        # self.source.set_property("tcp-timeout", 2000000)
        # self.source.set_property("timeout", 2000000)

        try:
            scale.set_property("add-borders", True)
        except Exception:
            pass
        self._sync_video_scale_caps()

        self._apply_sink_overlay_handle(sink, int(self.winId()))
        logger.debug(f"Configured display sink ({self._video_sink_factory}), sync=False, aspect-ratio=best-effort")

        self.pipeline.add(self.source)
        self.pipeline.add(rtph264depay)
        self.pipeline.add(h264parse)
        self.pipeline.add(self.tee)
        self.pipeline.add(decoder)
        self.pipeline.add(convert)
        self.pipeline.add(scale)
        self.pipeline.add(self._scale_capsfilter)
        self.pipeline.add(sink)

        rtph264depay.link(h264parse)
        h264parse.link(self.tee)
        
        # Create request pad for display sink
        tee_src_pad = self.tee.get_request_pad("src_%u")
        decoder_sink_pad = decoder.get_static_pad("sink")
        tee_src_pad.link(decoder_sink_pad)
        
        decoder.link(convert)
        convert.link(scale)
        scale.link(self._scale_capsfilter)
        self._scale_capsfilter.link(sink)
        
        # Add probe to count frames for FPS calculation
        sink_pad = convert.get_static_pad("sink")
        if sink_pad:
            def frame_probe_callback(pad, info):
                """Callback function to count frames."""
                self.increment_frame_count()
                return Gst.PadProbeReturn.OK
            
            sink_pad.add_probe(Gst.PadProbeType.BUFFER, frame_probe_callback)
            logger.debug("Added frame counting probe to convert element sink pad")
        else:
            logger.warning("Could not get sink pad for frame counting probe")

        # Add probe to count bytes for bitrate calculation (on h264parse sink - encoded data)
        h264_sink_pad = h264parse.get_static_pad("sink")
        if h264_sink_pad:
            def bytes_probe_callback(pad, info):
                """Callback to count bytes received for bitrate calculation."""
                buf = info.get_buffer()
                if buf:
                    self.bytes_received += buf.get_size()
                return Gst.PadProbeReturn.OK

            h264_sink_pad.add_probe(Gst.PadProbeType.BUFFER, bytes_probe_callback)
            logger.debug("Added byte counting probe to h264parse sink pad for bitrate")
        else:
            logger.warning("Could not get h264parse sink pad for bitrate probe")
        
        # Recording elements (will be added when recording starts)
        self.recording_queue = None
        self.recording_h264parse = None
        self.recording_mux = None
        self.recording_sink = None
        self.recording_tee_pad = None  # Store tee pad reference for cleanup
        self.is_recording = False
        self.recording_file_path = None
        self.recording_start_time = None  # Track when recording started
        self.auto_record_on_reconnect = False  # Flag to auto-start recording after reconnect
        self.is_stopping_recording = False  # Flag to prevent concurrent stop_recording calls

        def on_pad_added(element, pad):
            caps = pad.query_caps(None)
            name = caps.to_string()
            logger.debug(f"Pad added: {name}")
            if name.startswith("application/x-rtp"):
                sink_pad = rtph264depay.get_static_pad("sink")
                pad.link(sink_pad)
                logger.debug("Linked RTP pad to rtph264depay")
        self.source.connect("pad-added", on_pad_added)
        
        # Log pipeline structure
        logger.info("="*80)
        logger.info("GStreamer Pipeline:")
        logger.info("="*80)
        logger.info(f"Pipeline: {self.pipeline.get_name()}")
        elements = []
        it = self.pipeline.iterate_elements()
        while True:
            result, element = it.next()
            if result == Gst.IteratorResult.DONE:
                break
            if result == Gst.IteratorResult.OK:
                elements.append(element.get_name())
        logger.info(f"Elements: {' -> '.join(elements)}")
        logger.info(f"Pipeline description: rtspsrc -> rtph264depay -> h264parse -> avdec_h264 -> videoconvert -> videoscale -> capsfilter -> {self._video_sink_factory}")
        logger.info("="*80)
        logger.debug("Pipeline creation completed")

    def open_stream(self, URL_index, max_tries=None):
        logger.debug(f"open_stream called: URL_index={URL_index}, max_tries={max_tries}, current_state={self.state}")
        if self.state == VideoState.STATE_OPEN:
            logger.warning("Video is already open, ignoring open_stream request")
            return

        self._playback_intent = True
        url = URLs[URL_index]["url"]
        url_name = URLs[URL_index].get("name", "Unknown")
        playable_url = to_playable_url(url)
        logger.info(f"Opening stream: {url_name} at URL: {url}")
        self.source.set_property("location", playable_url)
        logger.debug(f"Set rtspsrc location to: {playable_url}")
        # Reset bitrate counters for new stream
        self.bytes_received = 0
        self.bitrate_last_bytes = 0
        self.total_frames = 0
        self.fps_last_frames = 0
        
        # Reset calculated values
        self.current_bitrate_mbps = 0.0
        self.current_fps = 0.0
        
        self.metrics_last_time = time.time()
        
        self._attach_video_sink()
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        logger.debug(f"Pipeline set_state(PLAYING) returned: {ret}")
        self.__change_state(VideoState.STATE_CONNECTING)

    def close_stream(self):
        logger.debug(f"close_stream called: current_state={self.state}, is_recording={self.is_recording}")
        if self.state == VideoState.STATE_CLOSE:
            logger.warning("Video is already closed, ignoring close_stream request")
            return
        elif self.state == VideoState.STATE_OPEN or self.state == VideoState.STATE_CONNECTING:
            logger.info("Closing stream...")
            # Stops reconnect scheduling before any synchronous pipeline work.
            self._playback_intent = False
            try:
                self._reconnect_timer.stop()
            except Exception:
                pass

            if self.is_recording:
                logger.info("Stopping active recording before closing stream")
                self.stop_recording()
            self.auto_record_on_reconnect = False  # Clear flag when manually closing

            with self._gst_pipeline_lock:
                self._detach_video_sink()
                try:
                    self.pipeline.set_state(Gst.State.READY)
                    self.pipeline.get_state(Gst.SECOND)
                except Exception as e:
                    logger.debug(f"READY transition during close: {e}")
                try:
                    ret = self.pipeline.set_state(Gst.State.NULL)
                    logger.debug(f"Pipeline set_state(NULL) returned: {ret}")
                    self.pipeline.get_state(5 * Gst.SECOND)
                except Exception as e:
                    logger.error(f"Pipeline NULL during close: {e}", exc_info=True)

                self.__change_state(VideoState.STATE_CLOSE)
                logger.info("Stream closed successfully")

    def _overlay_pixel_size(self):
        """Widget size in native/drawable pixels (HiDPI-aware)."""
        dpr = max(1.0, self.devicePixelRatioF())
        return max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))

    def _sync_video_scale_caps(self):
        """Scale decoded frames to the widget size before the display sink."""
        capsfilter = getattr(self, "_scale_capsfilter", None)
        if capsfilter is None:
            return
        w, h = self._overlay_pixel_size()
        capsfilter.set_property("caps", Gst.Caps.from_string(f"video/x-raw,width={w},height={h}"))

    def _sync_video_overlay_geometry(self):
        """Tell the GStreamer overlay the drawable size so video scales with the widget."""
        sink = getattr(self, "_video_sink", None)
        if sink is None or int(self.winId()) == 0:
            return
        w, h = self._overlay_pixel_size()
        if w <= 0 or h <= 0:
            return
        self._sync_video_scale_caps()
        try:
            GstVideo.VideoOverlay.set_render_rectangle(sink, 0, 0, w, h)
            GstVideo.VideoOverlay.expose(sink)
        except Exception as e:
            logger.debug(f"video overlay geometry: {e}")

    def _apply_sink_overlay_handle(self, sink, wid):
        """Set native window handle on the video sink (VideoOverlay preferred for glimagesink)."""
        if sink is None:
            return False
        try:
            GstVideo.VideoOverlay.set_window_handle(sink, wid)
            if wid:
                self._sync_video_overlay_geometry()
            return True
        except Exception as e:
            logger.debug(f"GstVideo.VideoOverlay unavailable ({e}); using set_window_handle")
            try:
                sink.set_window_handle(wid)
                if wid:
                    self._sync_video_overlay_geometry()
                return True
            except Exception as e2:
                logger.warning(f"glimagesink window handle failed: {e2}")
                return False

    def _detach_video_sink(self):
        """Unbind GL sink from the Qt window before destroying the pipeline (avoids compositor/GPU crashes)."""
        sink = getattr(self, "_video_sink", None)
        if sink is None and getattr(self, "pipeline", None):
            sink = self.pipeline.get_by_name("sink")
        try:
            self._apply_sink_overlay_handle(sink, 0)
        except Exception as e:
            logger.debug(f"detach glimagesink: {e}")

    def _attach_video_sink(self):
        """Bind glimagesink to this widget — required again after NULL->PLAYING or window changes."""
        sink = getattr(self, "_video_sink", None)
        if sink is None and getattr(self, "pipeline", None):
            sink = self.pipeline.get_by_name("sink")
        if sink is None:
            return
        wid = int(self.winId())
        if wid == 0:
            logger.warning("Video widget winId is 0; sink may stay black until widget is shown")
        ok = self._apply_sink_overlay_handle(sink, wid)
        logger.debug(f"video sink overlay handle wid={wid} ok={ok}")
        self.update()

    def _calculate_metrics(self):
        """Calculate metrics and emit signal."""
        # Calculate time delta
        current_time = time.time()
        time_delta = current_time - self.metrics_last_time
        
        if time_delta <= 0:
            return

        # Calculate FPS
        # Frames delta / time delta
        current_frames = self.total_frames
        frames_delta = current_frames - self.fps_last_frames
        self.current_fps = frames_delta / time_delta
        
        # Calculate Bitrate
        # Bytes delta * 8 / (1024*1024) / time delta
        current_bytes = self.bytes_received
        bytes_delta = current_bytes - self.bitrate_last_bytes
        self.current_bitrate_mbps = (bytes_delta * 8) / (1024 * 1024) / time_delta
        
        # Update last values
        self.fps_last_frames = current_frames
        self.bitrate_last_bytes = current_bytes
        self.metrics_last_time = current_time
        
        # Emit signal to player
        self.sig_metrics_update.emit(self.current_fps, self.current_bitrate_mbps)
    
    def increment_frame_count(self):
        """Increment total frame count."""
        self.total_frames += 1

    def start_metrics_timer(self):
        """Start the metrics update timer."""
        if not self._metrics_timer.isActive():
            # Reset counters to avoid stale data spikes
            self.metrics_last_time = time.time()
            self.fps_last_frames = self.total_frames
            self.bitrate_last_bytes = self.bytes_received
            
            self._metrics_timer.start(int(METRICS_UPDATE_PERIOD * 1000))
            logger.info("Metrics timer started")

    def stop_metrics_timer(self):
        """Stop the metrics update timer."""
        if self._metrics_timer.isActive():
            self._metrics_timer.stop()
            logger.info("Metrics timer stopped")

    
    def get_fps(self):
        """Get the current FPS value."""
        return self.current_fps

    def get_bitrate_mbps(self):
        """Get current receive bitrate in Mbps."""
        return self.current_bitrate_mbps

    def _handle_disconnect_with_recording(self):
        """Handle disconnect when recording is active - stop and save recording, set auto-restart flag."""
        if self.is_recording:
            logger.info("Stopping and saving recording due to disconnect...")
            # Set flag to auto-restart recording after reconnect
            self.auto_record_on_reconnect = True
            # Stop recording (this will save it)
            # This method should be called from main thread context
            self.stop_recording()
    
    def start_recording(self, file_path=None):
        """Start recording the video stream to a file."""
        logger.debug(f"start_recording called: file_path={file_path}, state={self.state}, is_recording={self.is_recording}")
        if self.state != VideoState.STATE_OPEN:
            logger.warning(f"Cannot start recording: stream is not open (state={self.state})")
            return False
        
        if self.is_recording:
            logger.warning("Recording is already in progress, ignoring start_recording request")
            return False
        
        # Clean up any leftover recording elements from previous recording
        # This ensures we can start a new recording even if the previous one wasn't fully cleaned up
        if self.recording_tee_pad is not None or self.recording_queue is not None:
            logger.warning("Found leftover recording elements from previous recording, cleaning up...")
            try:
                # Force cleanup of any remaining recording elements
                if self.recording_tee_pad and self.tee:
                    try:
                        peer = self.recording_tee_pad.get_peer()
                        if peer:
                            self.recording_tee_pad.unlink(peer)
                        self.recording_tee_pad.set_active(False)
                        self.tee.release_request_pad(self.recording_tee_pad)
                    except Exception as e:
                        logger.warning(f"Error releasing leftover tee pad: {e}")
                    self.recording_tee_pad = None
                self._cleanup_recording_elements()
                # Reset recording state
                self.is_recording = False
                self.recording_file_path = None
                self.recording_start_time = None
                logger.info("Cleaned up leftover recording elements")
            except Exception as e:
                logger.error(f"Error during cleanup of leftover elements: {e}", exc_info=True)
        
        # Generate file path if not provided
        if file_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # Create recordings directory if it doesn't exist
            recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
            os.makedirs(recordings_dir, exist_ok=True)
            logger.debug(f"Recordings directory: {recordings_dir}")
            # Try MP4 first, but we'll use MKV if mp4mux fails
            file_path = os.path.join(recordings_dir, f"recording_{timestamp}.mkv")
            logger.debug(f"Generated recording file path: {file_path}")
        else:
            logger.debug(f"Using provided recording file path: {file_path}")
        
        self.recording_file_path = file_path
        logger.info(f"Starting recording to: {file_path}")
        
        try:
            logger.debug("Creating recording pipeline elements...")
            # Create recording elements - record the encoded H.264 stream
            self.recording_queue = Gst.ElementFactory.make("queue", "recording_queue")
            # Add another h264parse to ensure proper format for muxer
            self.recording_h264parse = Gst.ElementFactory.make("h264parse", "recording_h264parse")
            logger.debug("Created recording_queue and recording_h264parse")
            
            # Determine output format and muxer
            # Use mpegtsmux for continuous recording (no segments, better for long recordings)
            # Fallback to matroskamux or mp4mux if mpegtsmux not available
            use_mp4 = file_path.endswith('.mp4')
            use_mkv = file_path.endswith('.mkv')
            
            # Try mpegtsmux first (best for continuous recording without segments)
            self.recording_mux = Gst.ElementFactory.make("mpegtsmux", "recording_mux")
            if self.recording_mux:
                # mpegtsmux doesn't create segments, perfect for continuous recording
                if not file_path.endswith('.ts') and not file_path.endswith('.mts'):
                    # Change extension to .ts for mpegtsmux
                    file_path = file_path.rsplit('.', 1)[0] + '.ts'
                    self.recording_file_path = file_path
                    logger.debug(f"Changed file extension to .ts for mpegtsmux: {file_path}")
                logger.info("Using mpegtsmux for continuous recording")
            else:
                # Fallback to matroskamux or mp4mux
                if use_mp4:
                    self.recording_mux = Gst.ElementFactory.make("mp4mux", "recording_mux")
                    if not self.recording_mux:
                        logger.warning("mp4mux not available, using matroskamux instead")
                        self.recording_mux = Gst.ElementFactory.make("matroskamux", "recording_mux")
                        file_path = file_path.replace('.mp4', '.mkv')
                        self.recording_file_path = file_path
                        logger.info("Using matroskamux (fallback from mp4mux)")
                    else:
                        logger.info("Using mp4mux")
                        # Configure mp4mux for continuous recording (not streamable)
                        try:
                            self.recording_mux.set_property("streamable", False)
                            logger.debug("Set mp4mux streamable=False")
                        except Exception as e:
                            logger.debug(f"Could not set mp4mux streamable property: {e}")
                        try:
                            self.recording_mux.set_property("fragment-duration", 0)
                            logger.debug("Set mp4mux fragment-duration=0")
                        except Exception as e:
                            logger.debug(f"Could not set mp4mux fragment-duration property: {e}")
                else:
                    self.recording_mux = Gst.ElementFactory.make("matroskamux", "recording_mux")
                    logger.info("Using matroskamux")
                    # Configure matroskamux for continuous recording
                    try:
                        self.recording_mux.set_property("streamable", False)
                        logger.debug("Set matroskamux streamable=False")
                    except Exception as e:
                        logger.debug(f"Could not set matroskamux streamable property: {e}")
                    try:
                        self.recording_mux.set_property("writing-app", "stream_subscriber")
                        logger.debug("Set matroskamux writing-app=stream_subscriber")
                    except Exception as e:
                        logger.debug(f"Could not set matroskamux writing-app property: {e}")
            
            self.recording_sink = Gst.ElementFactory.make("filesink", "recording_sink")
            
            if not all([self.recording_queue, self.recording_h264parse, self.recording_mux, self.recording_sink]):
                logger.error("Failed to create recording elements")
                missing = []
                if not self.recording_queue: missing.append("queue")
                if not self.recording_h264parse: missing.append("h264parse")
                if not self.recording_mux: missing.append("muxer")
                if not self.recording_sink: missing.append("filesink")
                logger.error(f"Missing elements: {', '.join(missing)}")
                return False
            
            logger.debug("All recording elements created successfully")
            # Set file path
            self.recording_sink.set_property("location", file_path)
            logger.debug(f"Set filesink location to: {file_path}")
            
            # Configure queue for better buffering
            # Use Gst.CLOCK_TIME_NONE (2^64-1) for unlimited time instead of 0
            self.recording_queue.set_property("max-size-buffers", 200)
            self.recording_queue.set_property("max-size-time", Gst.CLOCK_TIME_NONE)  # Unlimited time
            self.recording_queue.set_property("max-size-bytes", 0)  # 0 means unlimited bytes
            self.recording_queue.set_property("leaky", 0)  # No leaky - don't drop buffers, block instead
            logger.debug("Configured recording queue: max-size-buffers=200, max-size-time=CLOCK_TIME_NONE, leaky=0")
            
            # Add elements to pipeline
            logger.debug("Adding recording elements to pipeline...")
            self.pipeline.add(self.recording_queue)
            self.pipeline.add(self.recording_h264parse)
            self.pipeline.add(self.recording_mux)
            self.pipeline.add(self.recording_sink)
            logger.debug("Recording elements added to pipeline")
            
            # Link elements: tee -> queue -> h264parse -> mux -> filesink
            # Get a new src pad from tee (after h264parse, so we have encoded H.264)
            logger.debug("Linking recording pipeline elements...")
            
            # Verify tee element is available and in correct state
            if not self.tee:
                logger.error("Tee element is None, cannot create recording branch")
                self._cleanup_recording_elements()
                return False
            
            try:
                self.recording_tee_pad = self.tee.get_request_pad("src_%u")
                if not self.recording_tee_pad:
                    logger.error("Failed to get request pad from tee")
                    self._cleanup_recording_elements()
                    return False
                logger.debug("Got request pad from tee")
            except Exception as e:
                logger.error(f"Exception getting tee pad: {e}", exc_info=True)
                self._cleanup_recording_elements()
                return False
            
            queue_sink_pad = self.recording_queue.get_static_pad("sink")
            if not queue_sink_pad:
                logger.error("Failed to get sink pad from recording queue")
                if self.recording_tee_pad and self.tee:
                    try:
                        self.tee.release_request_pad(self.recording_tee_pad)
                    except:
                        pass
                    self.recording_tee_pad = None
                self._cleanup_recording_elements()
                return False
            
            # Link with proper caps negotiation
            try:
                link_result = self.recording_tee_pad.link(queue_sink_pad)
                if link_result != Gst.PadLinkReturn.OK:
                    logger.error(f"Failed to link tee pad to recording queue: {link_result}")
                    # Clean up on failure
                    if self.recording_tee_pad and self.tee:
                        try:
                            peer = self.recording_tee_pad.get_peer()
                            if peer:
                                self.recording_tee_pad.unlink(peer)
                            self.recording_tee_pad.set_active(False)
                            self.tee.release_request_pad(self.recording_tee_pad)
                        except Exception as e:
                            logger.warning(f"Error releasing tee pad after link failure: {e}")
                        self.recording_tee_pad = None
                    self._cleanup_recording_elements()
                    return False
                logger.info(f"Successfully linked tee pad to recording queue: {link_result}")
            except Exception as e:
                logger.error(f"Exception linking tee pad: {e}", exc_info=True)
                # Clean up on exception
                if self.recording_tee_pad and self.tee:
                    try:
                        self.tee.release_request_pad(self.recording_tee_pad)
                    except:
                        pass
                    self.recording_tee_pad = None
                self._cleanup_recording_elements()
                return False
            
            # Check pad capabilities to verify data flow (non-blocking, don't wait if not ready)
            try:
                logger.debug("Checking pad capabilities...")
                tee_caps = self.recording_tee_pad.get_current_caps()
                queue_caps = queue_sink_pad.get_current_caps()
                if tee_caps:
                    logger.debug(f"Tee pad caps: {tee_caps.to_string()}")
                else:
                    logger.debug("Tee pad caps: None (not ready yet)")
                if queue_caps:
                    logger.debug(f"Queue sink pad caps: {queue_caps.to_string()}")
                else:
                    logger.debug("Queue sink pad caps: None (not ready yet)")
            except Exception as e:
                logger.debug(f"Could not get pad caps (non-critical): {e}")
            
            # Link elements and check for success
            logger.debug("Linking recording queue to h264parse...")
            try:
                link_result = self.recording_queue.link(self.recording_h264parse)
                if link_result != True:
                    logger.error(f"Failed to link recording queue to h264parse: {link_result}")
                    # Clean up on failure
                    if self.recording_tee_pad and self.tee:
                        try:
                            peer = self.recording_tee_pad.get_peer()
                            if peer:
                                self.recording_tee_pad.unlink(peer)
                            self.recording_tee_pad.set_active(False)
                            self.tee.release_request_pad(self.recording_tee_pad)
                        except Exception as e:
                            logger.warning(f"Error releasing tee pad after link failure: {e}")
                        self.recording_tee_pad = None
                    self._cleanup_recording_elements()
                    return False
                logger.debug("Linked recording queue to h264parse")
            except Exception as e:
                logger.error(f"Exception linking queue to h264parse: {e}", exc_info=True)
                # Clean up on exception
                if self.recording_tee_pad and self.tee:
                    try:
                        self.tee.release_request_pad(self.recording_tee_pad)
                    except:
                        pass
                    self.recording_tee_pad = None
                self._cleanup_recording_elements()
                return False
            
            logger.debug("Linking recording h264parse to mux...")
            try:
                link_result = self.recording_h264parse.link(self.recording_mux)
                if link_result != True:
                    logger.error(f"Failed to link recording h264parse to mux: {link_result}")
                    # Clean up on failure
                    if self.recording_tee_pad and self.tee:
                        try:
                            peer = self.recording_tee_pad.get_peer()
                            if peer:
                                self.recording_tee_pad.unlink(peer)
                            self.recording_tee_pad.set_active(False)
                            self.tee.release_request_pad(self.recording_tee_pad)
                        except Exception as e:
                            logger.warning(f"Error releasing tee pad after link failure: {e}")
                        self.recording_tee_pad = None
                    self._cleanup_recording_elements()
                    return False
                logger.debug("Linked recording h264parse to mux")
            except Exception as e:
                logger.error(f"Exception linking h264parse to mux: {e}", exc_info=True)
                # Clean up on exception
                if self.recording_tee_pad and self.tee:
                    try:
                        self.tee.release_request_pad(self.recording_tee_pad)
                    except:
                        pass
                    self.recording_tee_pad = None
                self._cleanup_recording_elements()
                return False
            
            logger.debug("Linking recording mux to sink...")
            try:
                link_result = self.recording_mux.link(self.recording_sink)
                if link_result != True:
                    logger.error(f"Failed to link recording mux to sink: {link_result}")
                    # Clean up on failure
                    if self.recording_tee_pad and self.tee:
                        try:
                            peer = self.recording_tee_pad.get_peer()
                            if peer:
                                self.recording_tee_pad.unlink(peer)
                            self.recording_tee_pad.set_active(False)
                            self.tee.release_request_pad(self.recording_tee_pad)
                        except Exception as e:
                            logger.warning(f"Error releasing tee pad after link failure: {e}")
                        self.recording_tee_pad = None
                    self._cleanup_recording_elements()
                    return False
                logger.debug("Linked recording mux to sink")
            except Exception as e:
                logger.error(f"Exception linking mux to sink: {e}", exc_info=True)
                # Clean up on exception
                if self.recording_tee_pad and self.tee:
                    try:
                        self.tee.release_request_pad(self.recording_tee_pad)
                    except:
                        pass
                    self.recording_tee_pad = None
                self._cleanup_recording_elements()
                return False
            
            # Set elements to playing state and wait for completion
            logger.debug("Setting recording elements to PLAYING state...")
            
            # Verify main pipeline is in PLAYING state before starting recording elements
            ret, pipeline_state, pending = self.pipeline.get_state(Gst.SECOND)
            logger.debug(f"Main pipeline state before starting recording: {pipeline_state}, pending: {pending}")
            if pipeline_state != Gst.State.PLAYING:
                logger.warning(f"Main pipeline is not in PLAYING state ({pipeline_state}), this may cause recording to fail")
            
            ret = self.recording_queue.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("Failed to set recording queue to PLAYING")
                return False
            elif ret == Gst.StateChangeReturn.ASYNC:
                self.recording_queue.get_state(Gst.CLOCK_TIME_NONE)
            logger.debug("Recording queue set to PLAYING")
            
            ret = self.recording_h264parse.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("Failed to set recording h264parse to PLAYING")
                return False
            elif ret == Gst.StateChangeReturn.ASYNC:
                self.recording_h264parse.get_state(Gst.CLOCK_TIME_NONE)
            logger.debug("Recording h264parse set to PLAYING")
            
            ret = self.recording_mux.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("Failed to set recording mux to PLAYING")
                return False
            elif ret == Gst.StateChangeReturn.ASYNC:
                self.recording_mux.get_state(Gst.CLOCK_TIME_NONE)
            logger.debug("Recording mux set to PLAYING")
            
            ret = self.recording_sink.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("Failed to set recording sink to PLAYING")
                return False
            elif ret == Gst.StateChangeReturn.ASYNC:
                logger.debug("Recording sink state change is ASYNC, waiting for completion...")
                # Use a timeout to avoid infinite blocking
                timeout = 5.0  # 5 seconds timeout
                start_time = time.time()
                while (time.time() - start_time) < timeout:
                    ret_result, state, pending = self.recording_sink.get_state(Gst.SECOND)  # Check every second
                    if ret_result != Gst.StateChangeReturn.ASYNC:
                        logger.debug(f"Recording sink state change completed: {state}")
                        break
                    if state == Gst.State.PLAYING:
                        logger.debug("Recording sink reached PLAYING state")
                        break
                    time.sleep(0.1)
                else:
                    # Timeout reached
                    ret_result, state, pending = self.recording_sink.get_state(Gst.CLOCK_TIME_NONE)
                    logger.warning(f"Timeout waiting for recording sink to reach PLAYING. Current state: {state}, Pending: {pending}")
            else:
                logger.debug(f"Recording sink set to PLAYING (sync): {ret}")
            logger.debug("Recording sink set to PLAYING")
            
            logger.info("Recording pipeline elements set to PLAYING state")
            
            self.is_recording = True
            self.recording_start_time = time.time()
            self.sig_recording_changed.emit("recording")
            logger.info(f"Recording started successfully: {file_path}")
            logger.debug(f"Recording queue config: max-size-buffers=200, max-size-time={Gst.CLOCK_TIME_NONE}, leaky=0")
            if self.recording_mux:
                muxer_name = self.recording_mux.get_factory().get_name()
                logger.info(f"Using muxer: {muxer_name}")
                try:
                    props = []
                    for prop_name in ['streamable', 'fragment-duration']:
                        try:
                            val = self.recording_mux.get_property(prop_name)
                            props.append(f"{prop_name}={val}")
                        except:
                            pass
                    if props:
                        logger.debug(f"Muxer properties: {', '.join(props)}")
                except Exception as e:
                    logger.debug(f"Could not read muxer properties: {e}")
            
            # Start a monitoring thread to check recording status
            def monitor_recording():
                last_size = 0
                no_data_count = 0
                while self.is_recording:
                    elapsed = time.time() - self.recording_start_time if self.recording_start_time else 0
                    if self.recording_file_path and os.path.exists(self.recording_file_path):
                        file_size = os.path.getsize(self.recording_file_path)
                        if file_size == last_size and file_size == 0 and elapsed > 5:
                            no_data_count += 1
                            if no_data_count >= 2:  # After 10 seconds with no data
                                logger.warning(f"⚠️ WARNING: Recording file still 0 bytes after {elapsed:.1f}s! No data flowing to recording pipeline.")
                                # Check if recording elements are still in PLAYING state
                                try:
                                    if self.recording_queue:
                                        ret, state, pending = self.recording_queue.get_state(Gst.CLOCK_TIME_NONE)
                                        logger.warning(f"Recording queue state: {state}")
                                    if self.recording_mux:
                                        ret, state, pending = self.recording_mux.get_state(Gst.CLOCK_TIME_NONE)
                                        logger.warning(f"Recording mux state: {state}")
                                    if self.recording_sink:
                                        ret, state, pending = self.recording_sink.get_state(Gst.CLOCK_TIME_NONE)
                                        logger.warning(f"Recording sink state: {state}")
                                except Exception as e:
                                    logger.debug(f"Could not check element states: {e}")
                        else:
                            no_data_count = 0
                        if file_size != last_size:
                            logger.info(f"📹 Recording: {elapsed:.1f}s elapsed, file size: {file_size} bytes (+{file_size - last_size} bytes)")
                            last_size = file_size
                        else:
                            logger.info(f"📹 Recording: {elapsed:.1f}s elapsed, file size: {file_size} bytes (no change)")
                    time.sleep(5)  # Log status every 5 seconds
            
            monitor_thread = threading.Thread(target=monitor_recording)
            monitor_thread.daemon = True
            monitor_thread.start()
            logger.debug("Started recording monitoring thread")
            
            return True
            
        except Exception as e:
            logger.error(f"Error starting recording: {e}", exc_info=True)
            self._cleanup_recording_elements()
            return False
    
    def stop_recording(self):
        """Stop recording the video stream without affecting the main playback.
        
        This method only stops the recording branch of the pipeline. The main
        display pipeline continues to run normally, so the video stream will
        keep playing while recording stops.
        """
        logger.debug(f"stop_recording called: is_recording={self.is_recording}, is_stopping_recording={self.is_stopping_recording}, file_path={self.recording_file_path}")
        if not self.is_recording:
            logger.warning("Recording is not active, ignoring stop_recording request")
            return
        
        # Prevent concurrent calls to stop_recording
        if self.is_stopping_recording:
            logger.warning("Recording is already being stopped, ignoring duplicate stop_recording request")
            return
        
        self.is_stopping_recording = True
        
        try:
            saved_path = self.recording_file_path
            recording_duration = time.time() - self.recording_start_time if self.recording_start_time else 0
            logger.info(f"Stopping recording after {recording_duration:.2f} seconds: {saved_path}")
            
            # Update button to show "Saving" state
            self.sig_recording_changed.emit("saving")
            
            # Send EOS to the queue to stop data flow and propagate through the pipeline
            # The EOS will flow: queue -> h264parse -> mux -> sink
            if self.recording_queue:
                logger.debug("Sending EOS to recording pipeline...")
                self.recording_queue.send_event(Gst.Event.new_eos())
            else:
                logger.warning("Recording queue is None, cannot send EOS")
            
            # Wait for file to be finalized using a while loop
            # Check file size stabilization instead of bus messages to avoid interference
            logger.debug("Waiting for recording to finalize...")
            
            timeout = 5.0  # Maximum wait time in seconds
            start_time = time.time()
            file_finalized = False
            last_size = 0
            stable_count = 0
            
            # Wait for file size to stabilize (indicating muxer has finalized)
            while (time.time() - start_time) < timeout:
                if saved_path and os.path.exists(saved_path):
                    current_size = os.path.getsize(saved_path)
                    
                    # If file exists and has content
                    if current_size > 0:
                        # Check if size is stable (hasn't changed for 3 consecutive checks)
                        if current_size == last_size:
                            stable_count += 1
                            if stable_count >= 3:  # Stable for ~0.3 seconds
                                logger.debug(f"File size stabilized at {current_size} bytes, recording finalized")
                                file_finalized = True
                                # Give muxer a bit more time to write final headers
                                time.sleep(0.5)
                                break
                        else:
                            stable_count = 0
                            last_size = current_size
                            logger.debug(f"File size changing: {current_size} bytes")
                    else:
                        # File exists but is empty, wait a bit more
                        logger.debug("File exists but is empty, waiting...")
                        time.sleep(0.2)
                else:
                    # File doesn't exist yet, wait a bit
                    logger.debug("File doesn't exist yet, waiting...")
                    time.sleep(0.2)
                
                # Small sleep to avoid busy waiting
                time.sleep(0.1)
            
            if not file_finalized:
                logger.warning("Timeout waiting for file finalization, ensuring minimum wait time...")
                # Ensure we've waited at least 2 seconds for muxer to finalize
                elapsed = time.time() - start_time
                if elapsed < 2.0:
                    logger.debug(f"Waiting additional {2.0 - elapsed:.2f} seconds for muxer finalization")
                    time.sleep(2.0 - elapsed)
            
            # Now stop recording elements in reverse order (sink -> mux -> parse -> queue)
            # This ensures proper cleanup without affecting the main pipeline
            # IMPORTANT: Stop sink first to ensure file is flushed, then muxer
            logger.debug("Stopping recording elements...")
            
            # Helper function to wait for element to reach NULL state
            def wait_for_null_state(element, name):
                if not element:
                    logger.debug(f"{name} is None, skipping")
                    return
                logger.debug(f"Setting {name} to NULL state...")
                ret = element.set_state(Gst.State.NULL)
                if ret == Gst.StateChangeReturn.ASYNC:
                    # Wait for async state change to complete
                    timeout = 2.0
                    start = time.time()
                    while (time.time() - start) < timeout:
                        ret_result, state, pending = element.get_state(Gst.CLOCK_TIME_NONE)
                        if ret_result != Gst.StateChangeReturn.ASYNC:
                            break
                        if state == Gst.State.NULL:
                            break
                        time.sleep(0.1)
                    logger.debug(f"{name} set to NULL state (async)")
                elif ret == Gst.StateChangeReturn.FAILURE:
                    logger.warning(f"Failed to set {name} to NULL state")
                else:
                    logger.debug(f"{name} set to NULL state (sync)")
            
            # Stop elements in reverse order and wait for each to complete
            wait_for_null_state(self.recording_sink, "recording_sink")
            wait_for_null_state(self.recording_mux, "recording_mux")
            wait_for_null_state(self.recording_h264parse, "recording_h264parse")
            wait_for_null_state(self.recording_queue, "recording_queue")
            
            # Additional wait to ensure all state changes are complete
            time.sleep(0.3)
            logger.debug("All recording elements set to NULL state")
            
            # Now release the tee pad BEFORE removing elements
            # This ensures the pad is properly unlinked
            if self.recording_tee_pad and self.tee:
                logger.debug("Releasing recording tee pad...")
                # Unlink the pad first
                peer = self.recording_tee_pad.get_peer()
                if peer:
                    self.recording_tee_pad.unlink(peer)
                    logger.debug("Unlinked recording tee pad from peer")
                # Set pad to inactive
                self.recording_tee_pad.set_active(False)
                # Release the pad - this won't affect the main display branch
                self.tee.release_request_pad(self.recording_tee_pad)
                self.recording_tee_pad = None
                logger.debug("Released recording tee pad")
            
            # Now safely remove elements from pipeline (they should all be in NULL state)
            self._cleanup_recording_elements()
            
            # Verify main pipeline is still playing (should not be affected)
            # IMPORTANT: Check pipeline state BEFORE setting is_recording = False
            # because EOS handler checks is_recording flag
            logger.debug("Checking main pipeline state after stopping recording...")
            ret, state, pending = self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
            logger.debug(f"Main pipeline state after recording stop: {state}, pending: {pending}, application state: {self.state}")
            
            # Always ensure pipeline is in PLAYING if stream should be open
            if self.state == VideoState.STATE_OPEN:
                if state != Gst.State.PLAYING:
                    logger.warning(f"Main pipeline state is {state} (expected PLAYING), restarting to ensure video continues...")
                    # Set pipeline to NULL first, then to PLAYING to ensure clean restart
                    self.pipeline.set_state(Gst.State.NULL)
                    time.sleep(0.1)
                    ret = self.pipeline.set_state(Gst.State.PLAYING)
                    logger.debug(f"Pipeline set_state(PLAYING) returned: {ret}")
                    # Wait a bit for state change to propagate
                    time.sleep(0.3)
                    # Verify it reached PLAYING
                    ret, new_state, new_pending = self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
                    if new_state == Gst.State.PLAYING:
                        logger.info("Main pipeline successfully restarted to PLAYING state - video should continue")
                    else:
                        logger.warning(f"Main pipeline did not reach PLAYING state, current: {new_state}, pending: {new_pending}")
                        # Try one more time
                        self.pipeline.set_state(Gst.State.PLAYING)
                        time.sleep(0.2)
                else:
                    logger.debug("Main pipeline is still in PLAYING state, video should continue")
                    # Even if pipeline shows PLAYING, if EOS was received during recording,
                    # the stream might have stopped. Force a restart to ensure stream continues.
                    # This is safe because we're just restarting the same stream.
                    logger.debug("EOS was received during recording, forcing pipeline restart to ensure stream continues...")
                    self.pipeline.set_state(Gst.State.NULL)
                    time.sleep(0.1)
                    ret = self.pipeline.set_state(Gst.State.PLAYING)
                    logger.debug(f"Pipeline set_state(PLAYING) returned: {ret}")
                    time.sleep(0.3)
                    ret, final_state, final_pending = self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
                    if final_state == Gst.State.PLAYING:
                        logger.info("Pipeline restarted after recording stop to handle EOS - video should continue")
                    else:
                        logger.warning(f"Pipeline restart after recording stop: state={final_state}, pending={final_pending}")
            else:
                logger.debug(f"Application state is {self.state}, not checking/restarting pipeline")
            
            self.is_recording = False
            self.is_stopping_recording = False  # Clear stopping flag
            self.sig_recording_changed.emit("stopped")
            logger.info(f"Recording stopped: {saved_path}")
            self.recording_file_path = None
            
            # Verify file was created and has content
            if saved_path and os.path.exists(saved_path):
                file_size = os.path.getsize(saved_path)
                if file_size > 0:
                    logger.info(f"✅ Recording file saved successfully: {saved_path} ({file_size} bytes)")
                else:
                    logger.warning(f"⚠️ Warning: Recording file is empty: {saved_path}")
            else:
                logger.error(f"❌ Error: Recording file not found: {saved_path}")
            
        except Exception as e:
            logger.error(f"Error stopping recording: {e}", exc_info=True)
            self._cleanup_recording_elements()
            self.is_recording = False
            self.is_stopping_recording = False  # Clear stopping flag
            self.sig_recording_changed.emit("stopped")
    
    def _auto_start_recording_after_reconnect(self):
        """Helper method to auto-start recording after reconnect if it was enabled before disconnect."""
        logger.info(f"_auto_start_recording_after_reconnect called: auto_record_on_reconnect={self.auto_record_on_reconnect}, state={self.state}, is_recording={self.is_recording}")
        if self.auto_record_on_reconnect and self.state == VideoState.STATE_OPEN:
            # Wait a bit to ensure previous recording has fully stopped
            if self.is_recording:
                logger.warning("Previous recording still active, retrying auto-start in 1 second...")
                QTimer.singleShot(1000, self._auto_start_recording_after_reconnect)
                return
            logger.info("Auto-starting recording after reconnect...")
            self.auto_record_on_reconnect = False  # Clear flag before starting
            success = self.start_recording()
            if success:
                logger.info("✅ Auto-started recording after reconnect")
            else:
                logger.warning("⚠️ Failed to auto-start recording after reconnect")
                # Retry once after a delay
                QTimer.singleShot(2000, lambda: self._retry_auto_start_recording())
        else:
            logger.debug(f"Not auto-starting recording: auto_record_on_reconnect={self.auto_record_on_reconnect}, state={self.state}")
    
    def _retry_auto_start_recording(self):
        """Retry auto-starting recording if state is still open."""
        if self.state == VideoState.STATE_OPEN and not self.is_recording:
            logger.info("Retrying auto-start recording...")
            success = self.start_recording()
            if success:
                logger.info("✅ Auto-started recording after retry")
            else:
                logger.warning("⚠️ Failed to auto-start recording after retry")
    
    def _cleanup_recording_elements(self):
        """Remove recording elements from pipeline.
        
        IMPORTANT: Elements must be in NULL state before removal.
        This is ensured by calling this only after wait_for_null_state.
        """
        logger.debug("Cleaning up recording elements...")
        # Remove elements in reverse order of creation
        if self.recording_sink:
            try:
                # Double-check state before removal
                ret, state, pending = self.recording_sink.get_state(Gst.CLOCK_TIME_NONE)
                if state != Gst.State.NULL:
                    logger.warning(f"recording_sink is in {state} state, forcing NULL...")
                    self.recording_sink.set_state(Gst.State.NULL)
                    time.sleep(0.1)
                self.pipeline.remove(self.recording_sink)
                logger.debug("Removed recording_sink from pipeline")
            except Exception as e:
                logger.error(f"Error removing recording_sink: {e}")
            self.recording_sink = None
            
        if self.recording_mux:
            try:
                ret, state, pending = self.recording_mux.get_state(Gst.CLOCK_TIME_NONE)
                if state != Gst.State.NULL:
                    logger.warning(f"recording_mux is in {state} state, forcing NULL...")
                    self.recording_mux.set_state(Gst.State.NULL)
                    time.sleep(0.1)
                self.pipeline.remove(self.recording_mux)
                logger.debug("Removed recording_mux from pipeline")
            except Exception as e:
                logger.error(f"Error removing recording_mux: {e}")
            self.recording_mux = None
            
        if self.recording_h264parse:
            try:
                ret, state, pending = self.recording_h264parse.get_state(Gst.CLOCK_TIME_NONE)
                if state != Gst.State.NULL:
                    logger.warning(f"recording_h264parse is in {state} state, forcing NULL...")
                    self.recording_h264parse.set_state(Gst.State.NULL)
                    time.sleep(0.1)
                self.pipeline.remove(self.recording_h264parse)
                logger.debug("Removed recording_h264parse from pipeline")
            except Exception as e:
                logger.error(f"Error removing recording_h264parse: {e}")
            self.recording_h264parse = None
            
        if self.recording_queue:
            try:
                ret, state, pending = self.recording_queue.get_state(Gst.CLOCK_TIME_NONE)
                if state != Gst.State.NULL:
                    logger.warning(f"recording_queue is in {state} state, forcing NULL...")
                    self.recording_queue.set_state(Gst.State.NULL)
                    time.sleep(0.1)
                self.pipeline.remove(self.recording_queue)
                logger.debug("Removed recording_queue from pipeline")
            except Exception as e:
                logger.error(f"Error removing recording_queue: {e}")
            self.recording_queue = None
        
        logger.debug("Recording elements cleanup completed")

    def __init__(self):
        super().__init__()
        # Native window embedding for glimagesink (QOpenGLWidget uses an internal FBO — video stays black)
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # palette = self.palette()
        # palette.setColor(QPalette.Window, QColor(255, 0, 0))  # RGB color
        # self.setPalette(palette)
        # self.setAutoFillBackground(True)

        Gst.init(None)  # Initialize GStreamer

        self.__create_pipeline()
        
        # Initialize FPS tracking
        self.total_frames = 0
        self.fps_last_frames = 0
        self.current_fps = 0.0

        # Initialize bitrate tracking (bytes received from stream)
        self.bytes_received = 0
        self.bitrate_last_bytes = 0
        self.metrics_last_time = time.time()
        self.current_bitrate_mbps = 0.0
        
        # Connect signal for auto-starting recording (signal is thread-safe)
        self.sig_auto_start_recording.connect(self._auto_start_recording_after_reconnect)

        # Setup metrics timer (but don't start it yet - starts in STATE_OPEN)
        self._metrics_timer = QTimer()
        self._metrics_timer.timeout.connect(self._calculate_metrics)

        self.__change_state(VideoState.STATE_CLOSE)

        self._playback_intent = False
        self._gst_pipeline_lock = threading.Lock()
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._slot_pipeline_reconnect)
        self.sig_schedule_pipeline_reconnect.connect(
            self._on_schedule_pipeline_reconnect, Qt.QueuedConnection)

        self.bus_thread = threading.Thread(target=self.pipeline_bus_check)
        self.bus_thread.daemon = True  # Allow main application to exit even if thread is still running
        self.bus_thread.start()

    @staticmethod
    def _reconnect_backoff_ms(attempt):
        """Exponential backoff (ms); attempt is 1-based failure count."""
        if attempt < 1:
            attempt = 1
        return min(30000, int(500 * (2 ** min(attempt - 1, 6))))

    @pyqtSlot(int)
    def _on_schedule_pipeline_reconnect(self, delay_ms):
        """Coalesce reconnects on the GUI thread — safe for GL sink."""
        if not self._playback_intent:
            return
        self._reconnect_timer.stop()
        self._reconnect_timer.start(max(0, int(delay_ms)))

    @pyqtSlot()
    def _slot_pipeline_reconnect(self):
        with self._gst_pipeline_lock:
            if not self._playback_intent:
                return
            logger.warning("Main thread: reconnecting pipeline (NULL -> PLAYING)")
            try:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline.get_state(2 * Gst.SECOND)
            except Exception as e:
                logger.error(f"Error during reconnect NULL transition: {e}", exc_info=True)
            if not self._playback_intent:
                logger.debug("Reconnect aborted after NULL (user closed)")
                return
            self._attach_video_sink()
            try:
                ret = self.pipeline.set_state(Gst.State.PLAYING)
                logger.debug(f"Reconnect set_state(PLAYING): {ret}")
            except Exception as e:
                logger.error(f"Error during reconnect PLAYING: {e}", exc_info=True)

    @pyqtSlot()
    def _gst_main_stop_recording(self):
        self.stop_recording()

    @pyqtSlot()
    def _gst_main_state_open(self):
        if not self._playback_intent:
            logger.debug("Ignoring pipeline OPEN (shutdown or closed)")
            return
        self.__change_state(VideoState.STATE_OPEN)
        QTimer.singleShot(0, self._attach_video_sink)
        QTimer.singleShot(50, self._sync_video_overlay_geometry)

    @pyqtSlot()
    def _gst_main_state_close(self):
        self.__change_state(VideoState.STATE_CLOSE)

    def _marshal_stop_recording_from_bus_thread(self):
        QMetaObject.invokeMethod(self, "_gst_main_stop_recording", Qt.QueuedConnection)

    @pyqtSlot()
    def _gst_main_recording_teardown_before_reconnect(self):
        """Remove recording branch on GUI thread before pipeline reset (GL + pad safety)."""
        preserve_auto = getattr(self, "_reconnect_preserve_auto_record", False)
        try:
            if self.recording_tee_pad and self.tee:
                try:
                    peer = self.recording_tee_pad.get_peer()
                    if peer:
                        self.recording_tee_pad.unlink(peer)
                    self.recording_tee_pad.set_active(False)
                    self.tee.release_request_pad(self.recording_tee_pad)
                except Exception as e:
                    logger.warning(f"Error releasing tee pad (main thread): {e}")
                self.recording_tee_pad = None
            self._cleanup_recording_elements()
            self.is_recording = False
            self.is_stopping_recording = False
            if preserve_auto:
                self.auto_record_on_reconnect = True
                logger.info("Preserved auto_record_on_reconnect after main-thread recording teardown")
        except Exception as e:
            logger.error(f"recording teardown before reconnect failed: {e}", exc_info=True)

    def _marshal_recording_teardown_from_bus_thread(self, preserve_auto_record):
        self._reconnect_preserve_auto_record = preserve_auto_record
        QMetaObject.invokeMethod(
            self, "_gst_main_recording_teardown_before_reconnect", Qt.BlockingQueuedConnection)

    def _marshal_state_close_from_bus_thread(self):
        QMetaObject.invokeMethod(self, "_gst_main_state_close", Qt.QueuedConnection)

    def _marshal_state_open_from_bus_thread(self):
        QMetaObject.invokeMethod(self, "_gst_main_state_open", Qt.QueuedConnection)

    def pipeline_bus_check(self):
        logger.debug("Pipeline bus check thread started")
        bus = self.pipeline.get_bus()
        timeout_counter = 0
        reconnecting_counter = 0

        def sync_stop_recording_if_needed(kind):
            if self.is_recording and not self.is_stopping_recording:
                logger.info(f"Stopping and saving recording due to disconnect ({kind})...")
                self.auto_record_on_reconnect = True
                self.is_stopping_recording = True
                self._marshal_stop_recording_from_bus_thread()
                logger.debug("Waiting for recording to finish before reconnecting...")
                wait_start = time.time()
                while self.is_stopping_recording and (time.time() - wait_start) < 15:
                    time.sleep(0.5)
                if self.is_stopping_recording:
                    logger.warning("Timeout waiting for recording to stop, proceeding with reconnect anyway")
                    self.is_stopping_recording = False
            elif self.is_stopping_recording:
                logger.info("Recording is already being stopped, waiting for it to finish...")
                wait_start = time.time()
                while self.is_stopping_recording and (time.time() - wait_start) < 15:
                    time.sleep(0.5)
                if self.is_stopping_recording:
                    logger.warning("Timeout waiting for recording to stop, proceeding with reconnect anyway")
                    self.is_stopping_recording = False

        def teardown_recording_branch_on_main_if_needed(context):
            if self.is_recording and not self.is_stopping_recording:
                logger.warning(
                    f"Recording still active during reconnect ({context}), removing branch on main thread...")
                preserve_auto_record = self.auto_record_on_reconnect
                self._marshal_recording_teardown_from_bus_thread(preserve_auto_record)

        def schedule_reconnect_backoff():
            nonlocal reconnecting_counter
            if not self._playback_intent:
                return
            reconnecting_counter += 1
            delay_ms = self._reconnect_backoff_ms(reconnecting_counter)
            logger.warning(
                f"Scheduling reconnect on main thread (attempt {reconnecting_counter}, delay {delay_ms} ms)")
            self.sig_schedule_pipeline_reconnect.emit(delay_ms)

        while True:
            msg = bus.timed_pop_filtered(
                Gst.CLOCK_TIME_NONE,
                Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING
                | Gst.MessageType.STATE_CHANGED)
            if msg is None:
                time.sleep(0.02)
                continue
            if self.state == VideoState.STATE_CLOSE or self.state == VideoState.STATE_OPEN:
                timeout_counter = 0
            msg_type = msg.type
            if msg_type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                logger.error(f"❌ GStreamer Error: {err}, Debug: {debug}")
                sync_stop_recording_if_needed("ERROR")
                if self.state == VideoState.STATE_OPEN:
                    teardown_recording_branch_on_main_if_needed("ERROR")
                    schedule_reconnect_backoff()
                elif self.state == VideoState.STATE_CONNECTING:
                    timeout_counter += 1
                    if timeout_counter % 10 == 0:
                        logger.warning(
                            f"Still waiting for RTSP/connect (timeout_counter={timeout_counter}); "
                            "continuing backoff reconnect")
                    schedule_reconnect_backoff()
            elif msg_type == Gst.MessageType.EOS:
                src_name = getattr(msg.src, "name", "unknown") if msg.src else "unknown"
                logger.info(f"✅ End of Stream reached from element: {src_name}")
                sync_stop_recording_if_needed("EOS")
                if self.state == VideoState.STATE_OPEN:
                    teardown_recording_branch_on_main_if_needed("EOS")
                    schedule_reconnect_backoff()
                else:
                    logger.info("Stream ended, closing...")
                    self._marshal_state_close_from_bus_thread()
            elif msg_type == Gst.MessageType.WARNING:
                warn, debug = msg.parse_warning()
                logger.warning(f"⚠️ GStreamer Warning: {warn}, Debug: {debug}")
                if "Could not read from resource." in str(warn):
                    sync_stop_recording_if_needed("resource read error")
                    if self.state == VideoState.STATE_OPEN:
                        teardown_recording_branch_on_main_if_needed("resource read error")
                        schedule_reconnect_backoff()
                    elif self.state == VideoState.STATE_CONNECTING:
                        timeout_counter += 1
                        if timeout_counter % 10 == 0:
                            logger.warning(
                                f"Resource read stall while connecting (timeout_counter="
                                f"{timeout_counter}); continuing backoff reconnect")
                        schedule_reconnect_backoff()
            elif msg_type == Gst.MessageType.STATE_CHANGED:
                old_state, new_state, pending = msg.parse_state_changed()
                src = msg.src
                logger.debug(
                    f"🔄 State changed: {src.name if hasattr(src, 'name') else 'unknown'}: "
                    f"{old_state} → {new_state} (Pending: {pending})")
                if hasattr(src, 'name') and src.name == "rtsp-pipeline" and new_state == Gst.State.PLAYING:
                    timeout_counter = 0
                    reconnecting_counter = 0
                    logger.debug("Pipeline reached PLAYING state, resetting counters")
                    self._marshal_state_open_from_bus_thread()
            else:
                logger.debug(f"📢 Other Message: {msg_type}")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_video_overlay_geometry()

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, "_video_sink", None):
            if getattr(self, "_playback_intent", False):
                self._attach_video_sink()
            QTimer.singleShot(0, self._sync_video_overlay_geometry)

    def closeEvent(self, event):
        self.close_stream()

def _load_yaml(path):
    """Load YAML file, return None on error."""
    try:
        with open(path, 'r') as f:
            return yaml.safe_load(f)
    except (yaml.YAMLError, IOError) as e:
        logger.error(f"Error loading {path}: {e}")
        return None

def load_settings():
    """Load settings from configuration file (YAML format)."""
    logger.debug(f"Loading settings from {CONFIG_FILE}")
    # Copy default config if config file doesn't exist
    if not os.path.exists(CONFIG_FILE) and os.path.exists(DEFAULT_CONFIG_FILE):
        try:
            shutil.copy2(DEFAULT_CONFIG_FILE, CONFIG_FILE)
            logger.info(f"Created {CONFIG_FILE} from {DEFAULT_CONFIG_FILE}")
        except IOError as e:
            logger.error(f"Error copying default config file: {e}")
    
    # Load defaults from default config file, merge with DEFAULT_SETTINGS for any missing keys
    default_settings = None
    if os.path.exists(DEFAULT_CONFIG_FILE):
        default_settings = _load_yaml(DEFAULT_CONFIG_FILE)
        if default_settings:
            for k, v in DEFAULT_SETTINGS.items():
                if k not in default_settings:
                    default_settings[k] = v
    if default_settings is None:
        default_settings = {k: (list(v) if k == "urls" else v) for k, v in DEFAULT_SETTINGS.items()}
    
    # Load actual config file
    if os.path.exists(CONFIG_FILE):
        settings = _load_yaml(CONFIG_FILE)
        if settings:
            # Merge with defaults to ensure all keys exist
            for key in default_settings:
                if key not in settings:
                    settings[key] = default_settings[key]
            # Validate URLs structure
            if "urls" in settings and isinstance(settings["urls"], list):
                valid_urls = []
                for url_item in settings["urls"]:
                    if isinstance(url_item, dict) and "url" in url_item and "name" in url_item:
                        valid_urls.append(url_item)
                settings["urls"] = valid_urls if valid_urls else default_settings.get("urls", [])
            else:
                settings["urls"] = default_settings.get("urls", [])
            return settings
        logger.error(f"Error loading config file. Using defaults.")
    
    return default_settings

def save_settings(settings):
    """Save settings to configuration file (YAML format)."""
    logger.debug(f"Saving settings to {CONFIG_FILE}")
    try:
        with open(CONFIG_FILE, 'w') as f:
            yaml.dump(settings, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        logger.debug("Settings saved successfully")
    except IOError as e:
        logger.error(f"Error saving config file: {e}")

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
        
        # Add record button
        self.pushButton_Record = QPushButton("Record", self)
        self.pushButton_Record.setEnabled(False)  # Disabled until stream is open
        self.pushButton_Record.clicked.connect(self.on_record_button_clicked)

        # Add publish button
        self.pushButton_Publish = QPushButton("Publish to DB", self)
        self.pushButton_Publish.setEnabled(False)  # Disabled until InfluxDB is connected
        self.pushButton_Publish.clicked.connect(self.on_publish_button_clicked)

        # Add FPS label
        self.label_FPS = QLabel("FPS: 0.0", self)
        self.label_FPS.setStyleSheet("color: green; font-weight: bold;")

    
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
        pushButton_Open.setGeometry(FONT_SIZE_PIXELS, int(event.size().height()/2 - FONT_SIZE_PIXELS*1.2), FONT_SIZE_PIXELS * 8, FONT_SIZE_PIXELS * 2)
        # Position record button next to Open button
        self.pushButton_Record.setGeometry(pushButton_Open.x() + pushButton_Open.width() + FONT_SIZE_PIXELS, int(event.size().height()/2 - FONT_SIZE_PIXELS*1.2), FONT_SIZE_PIXELS * 8, FONT_SIZE_PIXELS * 2)
        # Position publish button next to Record button
        self.pushButton_Publish.setGeometry(self.pushButton_Record.x() + self.pushButton_Record.width() + FONT_SIZE_PIXELS, int(event.size().height()/2 - FONT_SIZE_PIXELS*1.2), FONT_SIZE_PIXELS * 10, FONT_SIZE_PIXELS * 2)
        # Position FPS label after Publish button
        self.label_FPS.setGeometry(self.pushButton_Publish.x() + self.pushButton_Publish.width() + FONT_SIZE_PIXELS, int(event.size().height()/2 - FONT_SIZE_PIXELS*1.2), FONT_SIZE_PIXELS * 6, FONT_SIZE_PIXELS * 2)
        # Adjust combo box to account for all buttons and FPS label
        comboBox_URL.setGeometry(self.label_FPS.x() + self.label_FPS.width() + FONT_SIZE_PIXELS, int(event.size().height()/2 - FONT_SIZE_PIXELS*1.2), event.size().width() - self.label_FPS.x() - self.label_FPS.width() - FONT_SIZE_PIXELS*2, FONT_SIZE_PIXELS * 2)
        line_Open.setGeometry(0, int(event.size().height() - FONT_SIZE_PIXELS), event.size().width(), line_Open.height())
    
    def on_record_button_clicked(self):
        """Handle record button click - will be connected to Video widget."""
        pass  # This will be handled by the Player class

    def on_publish_button_clicked(self):
        """Handle publish button click - will be connected to Player widget."""
        pass  # This will be handled by the Player class

    def sig_state_changed(self, state):
        comboBox_URL = self.findChild(QComboBox, "comboBox_URL")
        pushButton_Open = self.findChild(QPushButton, "pushButton_Open")
        if state == VideoState.STATE_OPEN:
            comboBox_URL.setEnabled(False)
            pushButton_Open.setEnabled(True)
            pushButton_Open.setText("Close")
            self.pushButton_Record.setEnabled(True)
        elif state == VideoState.STATE_CLOSE:
            comboBox_URL.setEnabled(True)
            pushButton_Open.setEnabled(True)
            pushButton_Open.setText("Open")
            self.pushButton_Record.setEnabled(False)
            self.pushButton_Record.setText("Record")
        elif state == VideoState.STATE_CONNECTING:
            comboBox_URL.setEnabled(False)
            pushButton_Open.setEnabled(True)
            pushButton_Open.setText("Connecting...")
            self.pushButton_Record.setEnabled(False)
    
    def sig_recording_changed(self, state):
        """Update record button text based on recording state."""
        if state == "recording":
            self.pushButton_Record.setText("Stop Recording")
            self.pushButton_Record.setStyleSheet("background-color: red; color: white;")
            # Button should already be enabled (stream is open)
        elif state == "saving":
            # Only update if not already in saving state (to avoid flicker)
            if self.pushButton_Record.text() != "Saving...":
                self.pushButton_Record.setText("Saving...")
                self.pushButton_Record.setStyleSheet("background-color: orange; color: white;")
                self.pushButton_Record.setEnabled(False)  # Disable button while saving
            # Force UI update
            QApplication.processEvents()
        else:  # "stopped"
            self.pushButton_Record.setText("Record")
            self.pushButton_Record.setStyleSheet("")
            # Re-enable button after saving (it was disabled during saving)
            # If stream is closed, start_recording will check and fail anyway
            self.pushButton_Record.setEnabled(True)

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

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.widgetOpen = Open(initial_url_index, urls_list)
        self.widgetOpen.setFixedHeight(FONT_SIZE_PIXELS * 4)
        layout.addWidget(self.widgetOpen)

        self.widgetVideo = Video()
        self.frame_player.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        frame_layout = QVBoxLayout(self.frame_player)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.addWidget(self.widgetVideo)
        layout.addWidget(self.frame_player, 1)

        pushButton_Open = self.widgetOpen.findChild(QPushButton, "pushButton_Open")
        pushButton_Open.clicked.connect(self.on_open_button_clicked)
        pushButton_Open.setEnabled(True)
        
        # Connect record button
        self.widgetOpen.pushButton_Record.clicked.connect(self.on_record_button_clicked)

        # Connect publish button
        self.widgetOpen.pushButton_Publish.clicked.connect(self.on_publish_button_clicked)
        
        self.widgetVideo.sig_state_changed.connect(self.widgetOpen.sig_state_changed)
        self.widgetVideo.sig_recording_changed.connect(self.widgetOpen.sig_recording_changed)
        self.widgetVideo.sig_metrics_update.connect(self.on_metrics_update)
        
        # Initialize FPS moving average tracking
        self.fps_history = []  # List to store recent FPS values
        self.moving_average_fps = 0.0  # Moving average FPS
        
        # Setup InfluxDB client
        self.influxdb_client = None
        self.write_api = None
        settings = load_settings()
        self.influxdb_url = settings.get("influxdb_url", "http://localhost:8086")
        self.influxdb_org = settings.get("influxdb_org", "fcclab")
        self.influxdb_bucket = settings.get("influxdb_bucket", "fcclab")
        self.influxdb_token = settings.get("influxdb_token", "fcclab_token")
        self.influxdb_measurement = settings.get("influxdb_measurement", "stream_metrics")
        if INFLUXDB_AVAILABLE:
            try:
                self.influxdb_client = InfluxDBClient(
                    url=self.influxdb_url,
                    token=self.influxdb_token,
                    org=self.influxdb_org
                )
                self.write_api = self.influxdb_client.write_api(write_precision="s")
                logger.info(f"Connected to InfluxDB at {self.influxdb_url}")
                # Enable publish button since InfluxDB is connected
                self.widgetOpen.pushButton_Publish.setEnabled(True)
            except Exception as e:
                logger.error(f"Failed to connect to InfluxDB: {e}")
                self.influxdb_client = None
                self.write_api = None
        
        # Publishing state
        self.is_publishing_continuous = False

    def on_metrics_update(self, fps, bitrate):
        """Update FPS label and publish to DB."""
        # Update moving average history
        if fps >= 0:
            self.fps_history.append(fps)
            # Keep only the last N samples
            if len(self.fps_history) > FPS_MOVING_AVERAGE_WINDOW:
                self.fps_history.pop(0)
            
            # Calculate moving average
            self.moving_average_fps = sum(self.fps_history) / len(self.fps_history)
        
        # Display moving average FPS
        self.widgetOpen.label_FPS.setText(f"FPS: {self.moving_average_fps:.1f}")

        # Publish FPS metrics to InfluxDB if continuous publishing is enabled
        if self.is_publishing_continuous:
            self.publish_metrics(fps, bitrate)
    
    def publish_metrics(self, fps, bitrate):
        """Publish metrics to InfluxDB."""
        if not self.write_api or not INFLUXDB_AVAILABLE:
            return
        
        try:
            # Get stream name from combobox
            comboBox_URL = self.widgetOpen.findChild(QComboBox, "comboBox_URL")
            stream_name = comboBox_URL.currentText() if comboBox_URL else "unknown"

            # Use Point API
            point = Point(self.influxdb_measurement) \
                .tag("stream", stream_name) \
                .field("average_fps", float(self.moving_average_fps)) \
                .field("current_fps", float(fps)) \
                .field("bitrate_mbps", float(bitrate)) \
                .field("x", 0)

            # Write to InfluxDB
            self.write_api.write(
                bucket=self.influxdb_bucket,
                org=self.influxdb_org,
                record=point
            )

        except Exception as e:
            logger_temp.error(f"Failed to publish metrics to InfluxDB: {e}")
            if self.influxdb_client:
                try:
                    self.influxdb_client.close()
                except Exception:
                    pass  # Ignore errors when closing
            self.influxdb_client = None  # Reset connection on error
            self.write_api = None


    def on_open_button_clicked(self):
        if self.widgetVideo.state == VideoState.STATE_OPEN or self.widgetVideo.state == VideoState.STATE_CONNECTING:
            self.widgetVideo.close_stream()
        else:
            comboBox_URL = self.widgetOpen.findChild(QComboBox, "comboBox_URL")
            index = comboBox_URL.currentIndex()
            url = self.urls_list[index]
            self.widgetVideo.open_stream(index)
    
    def on_record_button_clicked(self):
        """Handle record button click."""
        if self.widgetVideo.is_recording:
            # Immediately change button text to "Saving" when clicked
            self.widgetOpen.pushButton_Record.setText("Saving...")
            self.widgetOpen.pushButton_Record.setStyleSheet("background-color: orange; color: white;")
            self.widgetOpen.pushButton_Record.setEnabled(False)
            # Force UI update immediately - process events multiple times to ensure update
            QApplication.processEvents()
            QApplication.processEvents()
            # Use QTimer to defer stop_recording slightly to ensure UI updates first
            QTimer.singleShot(10, self.widgetVideo.stop_recording)
        else:
            self.widgetVideo.start_recording()

    def on_publish_button_clicked(self):
        """Handle publish button click - toggle continuous publishing to InfluxDB."""
        if self.is_publishing_continuous:
            # Stop continuous publishing
            self.is_publishing_continuous = False
            self.widgetOpen.pushButton_Publish.setText("Publish to DB")
            
            # Stop metrics timer
            self.widgetVideo.stop_metrics_timer()

            # Show status message
            self.show_status_message("Continuous publishing stopped", 2000)

            # Log the stop
            logger.info("Continuous publishing to InfluxDB stopped")
        else:
            # Start continuous publishing
            self.is_publishing_continuous = True
            self.widgetOpen.pushButton_Publish.setText("Stop Publishing")
            
            # Start metrics timer
            self.widgetVideo.start_metrics_timer()

            # Do an immediate publish to start
            try:
                self.publish_metrics(self.widgetVideo.get_fps(), self.widgetVideo.get_bitrate_mbps())
                self.show_status_message("Continuous publishing started", 2000)
                logger.info("Continuous publishing to InfluxDB started")
            except Exception as e:
                logger.error(f"Initial publish failed: {e}")
                self.show_status_message(f"Failed to start publishing: {str(e)}", 3000)
                # Reset state on failure
                self.is_publishing_continuous = False
                self.widgetOpen.pushButton_Publish.setText("Publish to DB")

    def show_status_message(self, message, timeout_milliseconds=None):
        """Show a message in the main window's status bar."""
        # Find the parent QMainWindow to access its status bar
        parent = self.parent()
        while parent and not hasattr(parent, 'statusBar'):
            parent = parent.parent()

        if parent and hasattr(parent, 'statusBar'):
            parent.statusBar().showMessage(message)
            if timeout_milliseconds:
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(timeout_milliseconds, lambda: parent.statusBar().clearMessage())

    # ------------------------------------------------------------------
    # REST API control surface (must run on Qt GUI thread)
    # ------------------------------------------------------------------

    def _combo(self):
        return self.widgetOpen.findChild(QComboBox, "comboBox_URL")

    def _refresh_topic_combo(self, select_index=None):
        combo = self._combo()
        if combo is None:
            return
        current = combo.currentIndex() if select_index is None else select_index
        combo.blockSignals(True)
        combo.clear()
        combo.addItems([u.get("name", u.get("url", f"topic-{i}")) for i, u in enumerate(self.urls_list)])
        if self.urls_list:
            combo.setCurrentIndex(max(0, min(current, len(self.urls_list) - 1)))
        combo.blockSignals(False)

    def _resolve_topic_index(self, index=None, name=None):
        if index is not None:
            if not (0 <= index < len(self.urls_list)):
                raise ValueError(f"Topic index out of range: {index}")
            return index
        if name is not None:
            for i, item in enumerate(self.urls_list):
                if item.get("name") == name:
                    return i
            raise ValueError(f"Topic name not found: {name}")
        combo = self._combo()
        return combo.currentIndex() if combo is not None else 0

    def api_list_topics(self):
        return [
            {"index": i, "name": u.get("name", ""), "url": u.get("url", "")}
            for i, u in enumerate(self.urls_list)
        ]

    def api_get_status(self):
        combo = self._combo()
        idx = combo.currentIndex() if combo is not None else 0
        if idx < 0 or idx >= len(self.urls_list):
            idx = 0
        item = self.urls_list[idx] if self.urls_list else {"name": "", "url": ""}
        state = getattr(self.widgetVideo, "state", VideoState.STATE_CLOSE)
        return {
            "state": state.name if hasattr(state, "name") else str(state),
            "recording": bool(getattr(self.widgetVideo, "is_recording", False)),
            "publishing": bool(self.is_publishing_continuous),
            "current_index": idx,
            "current_name": item.get("name", ""),
            "current_url": item.get("url", ""),
            "fps": float(getattr(self, "moving_average_fps", 0.0) or 0.0),
            "bitrate_mbps": float(self.widgetVideo.get_bitrate_mbps()) if hasattr(self.widgetVideo, "get_bitrate_mbps") else 0.0,
            "topics": self.api_list_topics(),
        }

    def api_add_topic(self, name, url):
        name = (name or "").strip()
        url = (url or "").strip()
        if not name or not url:
            raise ValueError("Both name and url are required")
        for item in self.urls_list:
            if item.get("url") == url or item.get("name") == name:
                raise ValueError(f"Topic already exists: {name} / {url}")
        self.urls_list.append({"name": name, "url": url})
        # Keep global URLs in sync if this is a different list object
        if self.urls_list is not URLs:
            URLs.clear()
            URLs.extend(self.urls_list)
        self._refresh_topic_combo(select_index=len(self.urls_list) - 1)
        settings = load_settings()
        settings["urls"] = list(self.urls_list)
        settings["url_index"] = len(self.urls_list) - 1
        save_settings(settings)
        logger.info(f"API added topic [{name}] {url}")
        return {"message": f"Added topic '{name}'", "index": len(self.urls_list) - 1}

    def api_remove_topic(self, index):
        if not (0 <= index < len(self.urls_list)):
            raise ValueError(f"Topic index out of range: {index}")
        if self.widgetVideo.state != VideoState.STATE_CLOSE:
            raise ValueError("Close the stream before removing a topic")
        removed = self.urls_list.pop(index)
        if self.urls_list is not URLs:
            URLs.clear()
            URLs.extend(self.urls_list)
        self._refresh_topic_combo(select_index=min(index, max(0, len(self.urls_list) - 1)))
        settings = load_settings()
        settings["urls"] = list(self.urls_list)
        settings["url_index"] = self._combo().currentIndex() if self._combo() else 0
        save_settings(settings)
        logger.info(f"API removed topic {removed}")
        return {"message": f"Removed topic '{removed.get('name', index)}'"}

    def api_select_topic(self, index=None, name=None):
        if self.widgetVideo.state != VideoState.STATE_CLOSE:
            raise ValueError("Close the stream before selecting a different topic")
        idx = self._resolve_topic_index(index=index, name=name)
        combo = self._combo()
        if combo is not None:
            combo.setCurrentIndex(idx)
        settings = load_settings()
        settings["url_index"] = idx
        save_settings(settings)
        item = self.urls_list[idx]
        return {"message": f"Selected topic '{item.get('name')}'", "index": idx}

    def api_open(self, index=None, name=None):
        if self.widgetVideo.state in (VideoState.STATE_OPEN, VideoState.STATE_CONNECTING):
            raise ValueError("Stream is already open or connecting; close it first")
        if not self.urls_list:
            raise ValueError("No topics configured")
        idx = self._resolve_topic_index(index=index, name=name)
        combo = self._combo()
        if combo is not None:
            combo.setCurrentIndex(idx)
        settings = load_settings()
        settings["url_index"] = idx
        save_settings(settings)
        self.widgetVideo.open_stream(idx)
        item = self.urls_list[idx]
        return {"message": f"Opening '{item.get('name')}'", "index": idx}

    def api_close(self):
        if self.widgetVideo.state == VideoState.STATE_CLOSE:
            return {"message": "Stream already closed"}
        self.widgetVideo.close_stream()
        return {"message": "Stream closed"}

    def api_record_start(self, path=None):
        if self.widgetVideo.state != VideoState.STATE_OPEN:
            raise ValueError("Stream must be open to start recording")
        if self.widgetVideo.is_recording:
            raise ValueError("Already recording")
        if path:
            self.widgetVideo.start_recording(path)
        else:
            self.widgetVideo.start_recording()
        return {"message": "Recording started"}

    def api_record_stop(self):
        if not self.widgetVideo.is_recording:
            raise ValueError("Not currently recording")
        self.widgetOpen.pushButton_Record.setText("Saving...")
        self.widgetOpen.pushButton_Record.setStyleSheet("background-color: orange; color: white;")
        self.widgetOpen.pushButton_Record.setEnabled(False)
        QApplication.processEvents()
        QTimer.singleShot(10, self.widgetVideo.stop_recording)
        return {"message": "Recording stop requested"}

    def api_publish_start(self):
        if self.is_publishing_continuous:
            return {"message": "Publishing already active"}
        if not self.write_api or not INFLUXDB_AVAILABLE:
            raise ValueError("InfluxDB is not available")
        self.is_publishing_continuous = True
        self.widgetOpen.pushButton_Publish.setText("Stop Publishing")
        self.widgetVideo.start_metrics_timer()
        try:
            self.publish_metrics(self.widgetVideo.get_fps(), self.widgetVideo.get_bitrate_mbps())
        except Exception as e:
            self.is_publishing_continuous = False
            self.widgetOpen.pushButton_Publish.setText("Publish to DB")
            raise ValueError(f"Failed to start publishing: {e}") from e
        return {"message": "Publishing started"}

    def api_publish_stop(self):
        if not self.is_publishing_continuous:
            return {"message": "Publishing already stopped"}
        self.is_publishing_continuous = False
        self.widgetOpen.pushButton_Publish.setText("Publish to DB")
        self.widgetVideo.stop_metrics_timer()
        return {"message": "Publishing stopped"}

    def resizeEvent(self, event):
        super().resizeEvent(event)
    
    def closeEvent(self, event):
        """Cleanup when Player is closed."""
        if self.influxdb_client:
            try:
                self.influxdb_client.close()
                logger.info("InfluxDB client closed")
            except Exception as e:
                logger.error(f"Error closing InfluxDB client: {e}")
        event.accept()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        loadUi("ui/Live.ui", self)

        # Load settings
        settings = load_settings()
        url_index = settings.get("url_index", 0)
        
        # Use global URLs (already loaded in main)
        self.player0 = Player(url_index, URLs)
        central = self.centralWidget()
        if central.layout() is None:
            central_layout = QVBoxLayout(central)
            central_layout.setContentsMargins(0, 0, 0, 0)
        else:
            central_layout = central.layout()
        central_layout.addWidget(self.player0)

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
        super().resizeEvent(event)
    
    def show_status_bar(self, message, timeout_miliseconds=None):
        self.statusBar().showMessage(message)
        if timeout_miliseconds is None:
            return
        QTimer.singleShot(timeout_miliseconds, lambda: self.statusBar().clearMessage())

if __name__ == '__main__':
    import argparse
    from api_server import DEFAULT_API_HOST, DEFAULT_API_PORT, StreamControlBridge, start_api_server

    parser = argparse.ArgumentParser(description="Stream subscriber with optional REST control API")
    parser.add_argument("--api-host", default=DEFAULT_API_HOST, help=f"REST API bind host (default: {DEFAULT_API_HOST})")
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT, help=f"REST API port (default: {DEFAULT_API_PORT})")
    parser.add_argument("--no-api", action="store_true", help="Disable the REST/Swagger control API")
    args, qt_args = parser.parse_known_args()

    _bootstrap_qt_for_gstreamer_embed()
    app = QApplication([sys.argv[0], *qt_args])

    FONT_SIZE_PIXELS = int(QWidget().font().pointSize() * app.primaryScreen().logicalDotsPerInch() / 72.0)
    logger.info(f"FONT_SIZE_PIXELS: {FONT_SIZE_PIXELS}")
    
    # Load settings and initialize global URLs
    # load_settings() will automatically copy from default config if needed
    settings = load_settings()
    # Update the global URLs list
    URLs.clear()
    URLs.extend(settings.get("urls", []))
    
    window = MainWindow()
    window.show()

    if not args.no_api:
        try:
            bridge = StreamControlBridge(window.player0)
            _thread, _server, docs_urls = start_api_server(
                bridge, host=args.api_host, port=args.api_port
            )
            # Prefer a LAN IP in the status bar when bound on all interfaces
            status_url = next(
                (u for u in docs_urls if not u.startswith("http://127.0.0.1")),
                docs_urls[0] if docs_urls else f"http://127.0.0.1:{args.api_port}/docs",
            )
            window.show_status_bar(f"REST API {status_url}", 12000)
        except Exception as e:
            logger.error(f"Failed to start control API: {e}")

    sys.exit(app.exec_())
