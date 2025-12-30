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
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler

import sys
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QOpenGLWidget
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GObject, GstVideo

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "stream_subscriber.conf")
DEFAULT_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "stream_subscriber.default.conf")
LOG_FILE = os.path.join(os.path.dirname(__file__), "stream_subscriber.log")

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

# Global URLs variable - will be initialized from config
URLs = []

FONT_SIZE_PIXELS = 0

class VideoState(Enum):
    STATE_CLOSE = 0
    STATE_CONNECTING = 1
    STATE_OPEN = 2

class Video(QOpenGLWidget):
    
    sig_state_changed = pyqtSignal(VideoState)
    sig_recording_changed = pyqtSignal(str)  # Signal for recording state changes: "recording", "saving", "stopped"

    def __change_state(self, state):
        current_state = getattr(self, 'state', None)
        logger.debug(f"__change_state called: current={current_state}, new={state}")
        self.state = state
        if self.state == VideoState.STATE_CLOSE:
            logger.info("Video state changed to CLOSE")
        elif self.state == VideoState.STATE_OPEN:
            logger.info("Video state changed to OPEN")
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
        sink = Gst.ElementFactory.make("glimagesink", "sink")

        if not all([self.source, rtph264depay, h264parse, self.tee, decoder, convert, sink]):
            logger.error("Failed to create GStreamer elements")
            missing = []
            if not self.source: missing.append("rtspsrc")
            if not rtph264depay: missing.append("rtph264depay")
            if not h264parse: missing.append("h264parse")
            if not self.tee: missing.append("tee")
            if not decoder: missing.append("avdec_h264")
            if not convert: missing.append("videoconvert")
            if not sink: missing.append("glimagesink")
            logger.error(f"Missing elements: {', '.join(missing)}")
        else:
            logger.debug("All GStreamer elements created successfully")

        self.source.set_property("latency", 100)  # Adjust latency for real-time streaming
        logger.debug("Set rtspsrc latency to 100ms")
        # self.source.set_property("tcp-timeout", 2000000)
        # self.source.set_property("timeout", 2000000)

        sink.set_property("force-aspect-ratio", True)
        sink.set_property("sync", False)
        sink.set_window_handle(self.winId())
        logger.debug("Configured glimagesink: force-aspect-ratio=True, sync=False")

        self.pipeline.add(self.source)
        self.pipeline.add(rtph264depay)
        self.pipeline.add(h264parse)
        self.pipeline.add(self.tee)
        self.pipeline.add(decoder)
        self.pipeline.add(convert)
        self.pipeline.add(sink)

        rtph264depay.link(h264parse)
        h264parse.link(self.tee)
        
        # Create request pad for display sink
        tee_src_pad = self.tee.get_request_pad("src_%u")
        decoder_sink_pad = decoder.get_static_pad("sink")
        tee_src_pad.link(decoder_sink_pad)
        
        decoder.link(convert)
        convert.link(sink)
        
        # Recording elements (will be added when recording starts)
        self.recording_queue = None
        self.recording_h264parse = None
        self.recording_mux = None
        self.recording_sink = None
        self.recording_tee_pad = None  # Store tee pad reference for cleanup
        self.is_recording = False
        self.recording_file_path = None
        self.recording_start_time = None  # Track when recording started

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
        logger.info(f"Pipeline description: rtspsrc -> rtph264depay -> h264parse -> avdec_h264 -> videoconvert -> glimagesink")
        logger.info("="*80)
        logger.debug("Pipeline creation completed")

    def open_stream(self, URL_index, max_tries=None):
        logger.debug(f"open_stream called: URL_index={URL_index}, max_tries={max_tries}, current_state={self.state}")
        if self.state == VideoState.STATE_OPEN:
            logger.warning("Video is already open, ignoring open_stream request")
            return

        url = URLs[URL_index]["url"]
        url_name = URLs[URL_index].get("name", "Unknown")
        logger.info(f"Opening stream: {url_name} at URL: {url}")
        self.source.set_property("location", url)
        logger.debug(f"Set rtspsrc location to: {url}")
        
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
            # Stop recording if active
            if self.is_recording:
                logger.info("Stopping active recording before closing stream")
                self.stop_recording()
            ret = self.pipeline.set_state(Gst.State.NULL)
            logger.debug(f"Pipeline set_state(NULL) returned: {ret}")
            self.__change_state(VideoState.STATE_CLOSE)
            logger.info("Stream closed successfully")
    
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
        logger.debug(f"stop_recording called: is_recording={self.is_recording}, file_path={self.recording_file_path}")
        if not self.is_recording:
            logger.warning("Recording is not active, ignoring stop_recording request")
            return
        
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
            self.sig_recording_changed.emit("stopped")
    
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
        logger.debug("Pipeline bus check thread started")
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
                logger.error(f"❌ GStreamer Error: {err}, Debug: {debug}")
                if (self.state == VideoState.STATE_OPEN):
                    reconnecting_counter += 1
                    logger.warning(f"Reconnecting (attempt {reconnecting_counter})...")
                    self.pipeline.set_state(Gst.State.NULL)
                    self.pipeline.set_state(Gst.State.PLAYING)
                    time.sleep(1)
                elif (self.state == VideoState.STATE_CONNECTING):
                    timeout_counter += 1
                    reconnecting_counter += 1
                    logger.warning(f"Reconnecting (attempt {reconnecting_counter}, timeout_counter={timeout_counter})...")
                    self.pipeline.set_state(Gst.State.NULL)
                    self.pipeline.set_state(Gst.State.PLAYING)
                    logger.debug(f"Timeout counter: {timeout_counter} over 10")
                    if timeout_counter > 10:
                        logger.error("Timeout reached, closing stream...")
                        self.pipeline.set_state(Gst.State.NULL)
                        self.__change_state(VideoState.STATE_CLOSE)
                    time.sleep(1)
            elif msg_type == Gst.MessageType.EOS:
                src_name = "unknown"
                if hasattr(msg.src, 'name'):
                    src_name = msg.src.name
                logger.info(f"✅ End of Stream reached from element: {src_name}")
                if self.is_recording:
                    logger.warning(f"EOS received while recording is active! This may stop the recording. Source: {src_name}")
                if (self.state == VideoState.STATE_OPEN):
                    reconnecting_counter += 1
                    logger.warning(f"Reconnecting after EOS (attempt {reconnecting_counter})...")
                    # Don't restart pipeline if recording is active - it will stop the recording
                    if not self.is_recording:
                        self.pipeline.set_state(Gst.State.NULL)
                        self.pipeline.set_state(Gst.State.PLAYING)
                        time.sleep(1)
                    else:
                        logger.warning("Skipping pipeline restart because recording is active")
                else:
                    logger.info("Stream ended, closing...")
                    self.__change_state(VideoState.STATE_CLOSE)
            elif msg_type == Gst.MessageType.WARNING:
                warn, debug = msg.parse_warning()
                logger.warning(f"⚠️ GStreamer Warning: {warn}, Debug: {debug}")
                if "Could not read from resource." in str(warn):
                    if (self.state == VideoState.STATE_OPEN):
                        reconnecting_counter += 1
                        logger.warning(f"Reconnecting after resource read error (attempt {reconnecting_counter})...")
                        self.pipeline.set_state(Gst.State.NULL)
                        self.pipeline.set_state(Gst.State.PLAYING)
                        time.sleep(1)
            elif msg_type == Gst.MessageType.STATE_CHANGED:
                old_state, new_state, pending = msg.parse_state_changed()
                src = msg.src  # The element that changed state
                logger.debug(f"🔄 State changed: {src.name if hasattr(src, 'name') else 'unknown'}: {old_state} → {new_state} (Pending: {pending})")
                if hasattr(src, 'name') and src.name == "rtsp-pipeline" and new_state == Gst.State.PLAYING:
                    timeout_counter = 0
                    reconnecting_counter = 0
                    logger.debug("Pipeline reached PLAYING state, resetting counters")
                    self.__change_state(VideoState.STATE_OPEN)
            else:
                logger.debug(f"📢 Other Message: {msg_type}")

    def resizeEvent(self, event):
        print(f"Video resized to: {event.size().width()}x{event.size().height()}")

    def closeEvent(self, event):
        self.close_stream()

def load_settings():
    """Load settings from configuration file."""
    logger.debug(f"Loading settings from {CONFIG_FILE}")
    # Copy default config if config file doesn't exist
    if not os.path.exists(CONFIG_FILE) and os.path.exists(DEFAULT_CONFIG_FILE):
        try:
            shutil.copy2(DEFAULT_CONFIG_FILE, CONFIG_FILE)
            logger.info(f"Created {CONFIG_FILE} from {DEFAULT_CONFIG_FILE}")
        except IOError as e:
            logger.error(f"Error copying default config file: {e}")
    
    # Load defaults from default config file
    default_settings = None
    if os.path.exists(DEFAULT_CONFIG_FILE):
        try:
            with open(DEFAULT_CONFIG_FILE, 'r') as f:
                default_settings = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Error loading default config file: {e}")
    
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
            logger.error(f"Error loading config file: {e}. Using defaults.")
            return default_settings
    
    return default_settings

def save_settings(settings):
    """Save settings to configuration file."""
    logger.debug(f"Saving settings to {CONFIG_FILE}")
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(settings, f, indent=4)
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
        # Position record button next to Open button
        self.pushButton_Record.setGeometry(pushButton_Open.x() + pushButton_Open.width() + FONT_SIZE_PIXELS, int(event.size().height()/2 - FONT_SIZE_PIXELS*1.2), FONT_SIZE_PIXELS * 10, FONT_SIZE_PIXELS * 2)
        # Adjust combo box to account for record button
        comboBox_URL.setGeometry(self.pushButton_Record.x() + self.pushButton_Record.width() + FONT_SIZE_PIXELS, int(event.size().height()/2 - FONT_SIZE_PIXELS*1.2), event.size().width() - self.pushButton_Record.x() - self.pushButton_Record.width() - FONT_SIZE_PIXELS*3, FONT_SIZE_PIXELS * 2)
        line_Open.setGeometry(0, int(event.size().height() - FONT_SIZE_PIXELS), event.size().width(), line_Open.height())
    
    def on_record_button_clicked(self):
        """Handle record button click - will be connected to Video widget."""
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

        layout = QVBoxLayout()
        self.setLayout(layout)

        self.widgetOpen = Open(initial_url_index, urls_list)
        self.layout().addWidget(self.widgetOpen)

        self.widgetVideo = Video()
        self.layout().addWidget(self.widgetVideo)

        pushButton_Open = self.widgetOpen.findChild(QPushButton, "pushButton_Open")
        pushButton_Open.clicked.connect(self.on_open_button_clicked)
        pushButton_Open.setEnabled(True)
        
        # Connect record button
        self.widgetOpen.pushButton_Record.clicked.connect(self.on_record_button_clicked)
        
        self.widgetVideo.sig_state_changed.connect(self.widgetOpen.sig_state_changed)
        self.widgetVideo.sig_recording_changed.connect(self.widgetOpen.sig_recording_changed)

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
    logger.info(f"FONT_SIZE_PIXELS: {FONT_SIZE_PIXELS}")
    
    # Load settings and initialize global URLs
    # load_settings() will automatically copy from default config if needed
    settings = load_settings()
    # Update the global URLs list
    URLs.clear()
    URLs.extend(settings.get("urls", []))
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
