#!/usr/bin/env python3
"""
Video Publisher - Stream video from V4L2 device to MediaMTX server via GStreamer Python bindings
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# Initialize GStreamer
Gst.init(None)

# Constants
RECONNECT_DELAY = 2
STALL_THRESHOLD = 5
DEFAULT_RTMP_TIMEOUT = 2
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEST_LIST = os.path.join(SCRIPT_DIR, 'dest.list')
DEST_LIST_DEFAULT = os.path.join(SCRIPT_DIR, 'dest.list.default')
DEFAULT_DEST_LIST = DEST_LIST  # user-editable list (seeded from dest.list.default)

# Prefer compressed MJPG at high res (higher fps / less USB bandwidth), then common raw formats.
FORMAT_PREFERENCE = ['MJPG', 'JPEG', 'YUYV', 'YUY2', 'NV12', 'RGB3', 'BGR3', 'UYVY', 'GREY', 'Y800']
# RealSense IR nodes expose UYVY as packed stereo IR (looks like colorful garbage as "color").
IR_FORMATS = {'Y8I', 'Y12I', 'Y16', 'GREY', 'Y800'}
DEPTH_FORMATS = {'Z16'}
COLOR_FORMATS = {'MJPG', 'JPEG', 'YUYV', 'YUY2', 'UYVY', 'NV12', 'RGB3', 'BGR3'}

# V4L2 fourcc -> (gst media type, gst raw format or None for compressed)
V4L_TO_GST = {
    'MJPG': ('image/jpeg', None),
    'JPEG': ('image/jpeg', None),
    'YUYV': ('video/x-raw', 'YUY2'),
    'YUY2': ('video/x-raw', 'YUY2'),
    'UYVY': ('video/x-raw', 'UYVY'),
    'NV12': ('video/x-raw', 'NV12'),
    'RGB3': ('video/x-raw', 'RGB'),
    'BGR3': ('video/x-raw', 'BGR'),
    'GREY': ('video/x-raw', 'GRAY8'),
    'Y800': ('video/x-raw', 'GRAY8'),
    'Y8I': ('video/x-raw', 'GRAY8'),  # treat as grey; stereo IR is special-cased below
}


def _normalize_fourcc(fmt):
    return (fmt or '').strip().upper()


def device_role(available_formats):
    """Classify capture node: color | ir | depth | unknown."""
    keys = {_normalize_fourcc(f) for f in available_formats}
    if keys & DEPTH_FORMATS:
        return 'depth'
    if keys & {'Y8I', 'Y12I', 'Y16'}:
        return 'ir'
    if keys & COLOR_FORMATS:
        return 'color'
    if keys & {'GREY', 'Y800'}:
        return 'ir'
    return 'unknown'


def get_device_card_name(device):
    """Best-effort V4L2 card name for display."""
    try:
        base = os.path.basename(device)
        sys_name = f'/sys/class/video4linux/{base}/name'
        if os.path.isfile(sys_name):
            with open(sys_name, encoding='utf-8', errors='ignore') as f:
                return f.read().strip()
    except OSError:
        pass
    try:
        result = subprocess.run(
            ['v4l2-ctl', '--device', device, '--info'],
            capture_output=True, text=True, check=False, timeout=2
        )
        for line in result.stdout.splitlines():
            if 'Card type' in line:
                return line.split(':', 1)[-1].strip()
    except Exception:
        pass
    return ''


def probe_device_formats(device):
    """
    Probe V4L2 device formats.

    Returns dict: { 'MJPG': ['1280x720', '640x480', ...], 'YUYV': [...], ... }
    FourCC keys are stripped/normalized (no trailing spaces).
    """
    sizes_by_format = {}

    try:
        result = subprocess.run(
            ['v4l2-ctl', '--device', device, '--list-formats-ext'],
            capture_output=True,
            text=True,
            check=False,
            timeout=3
        )

        if result.returncode == 0 and result.stdout:
            current_format = None
            sizes = []
            for line in result.stdout.split('\n'):
                line = line.strip()
                if line.startswith('[') and ':' in line:
                    if current_format and sizes:
                        sizes_by_format.setdefault(current_format, []).extend(sizes)

                    if "'" in line:
                        current_format = _normalize_fourcc(line.split("'")[1])
                        sizes = []
                elif line.startswith('Size:') and current_format:
                    size_info = line.replace('Size: Discrete ', '').replace('Size: Stepwise ', '').strip()
                    # Keep only WIDTHxHEIGHT token
                    match = re.search(r'(\d+x\d+)', size_info)
                    if match:
                        sizes.append(match.group(1))
                elif line.startswith('Interval:') and current_format and sizes:
                    sizes_by_format.setdefault(current_format, []).extend(sizes)
                    sizes = []

            if current_format and sizes:
                sizes_by_format.setdefault(current_format, []).extend(sizes)

        if not sizes_by_format:
            result2 = subprocess.run(
                ['v4l2-ctl', '--device', device, '--list-formats'],
                capture_output=True,
                text=True,
                check=False,
                timeout=3
            )
            if result2.returncode == 0 and result2.stdout:
                for line in result2.stdout.split('\n'):
                    if "'" in line:
                        fmt = _normalize_fourcc(line.split("'")[1])
                        sizes_by_format.setdefault(fmt, [])
    except Exception:
        pass

    return {fmt: sorted(set(sizes), key=lambda s: -_resolution_pixels(s))
            for fmt, sizes in sizes_by_format.items()}


def _resolution_pixels(resolution):
    try:
        w, h = resolution.lower().split('x')
        return int(w) * int(h)
    except (ValueError, AttributeError):
        return 0


def get_device_formats(device):
    """Get supported formats for display: ['MJPG (1280x720, ...)', ...]."""
    formats = []
    for fmt, sizes in probe_device_formats(device).items():
        unique_sizes = sizes[:5]
        if unique_sizes:
            formats.append(f"{fmt} ({', '.join(unique_sizes)})")
        else:
            formats.append(fmt)
    return formats


def detect_input_format(device, preferred_format=None, preferred_resolution=None):
    """
    Auto-detect capture format and resolution for a device.

    Returns (v4l_format, resolution) e.g. ('MJPG', '1280x720').
    """
    available = probe_device_formats(device)
    if not available:
        # Fall back to defaults if probe fails
        fmt = (preferred_format or 'YUYV').upper()
        res = preferred_resolution or '1280x720'
        print(f"Warning: could not probe {device}; using {fmt} {res}")
        return fmt, res

    role = device_role(available)
    fmt = None
    if preferred_format:
        candidate = _normalize_fourcc(preferred_format)
        # Accept GStreamer aliases
        aliases = {'YUY2': 'YUYV', 'GRAY8': 'GREY'}
        candidate = aliases.get(candidate, candidate)
        if candidate in available:
            fmt = candidate
        else:
            print(f"Warning: format {preferred_format} not supported by {device}; auto-detecting")

    if not fmt:
        if role == 'ir':
            # Prefer true greyscale over RealSense packed-IR UYVY (looks like colorful noise).
            for candidate in ['GREY', 'Y800', 'Y8I', 'Y12I']:
                if candidate in available:
                    fmt = candidate
                    break
            if fmt:
                print(f"Note: {device} looks like an IR node; using {fmt} (avoid packed UYVY)")
        elif role == 'depth':
            print(f"Warning: {device} looks like a depth node (Z16); color encode may look wrong")
            fmt = next(iter(available))

        if not fmt:
            for candidate in FORMAT_PREFERENCE:
                if candidate in available:
                    fmt = candidate
                    break
        if not fmt:
            fmt = next(iter(available))

    sizes = available.get(fmt, [])
    res = preferred_resolution
    if res and sizes and res not in sizes:
        # Pick closest area match, else largest
        target = _resolution_pixels(res)
        res = min(sizes, key=lambda s: abs(_resolution_pixels(s) - target))
        print(f"Warning: resolution {preferred_resolution} not available for {fmt}; using {res}")
    elif not res:
        res = sizes[0] if sizes else '1280x720'
    elif not sizes:
        res = preferred_resolution or '1280x720'

    return fmt, res


def build_source_caps(v4l_format, resolution):
    """Build GStreamer caps (+ optional jpeg decoder) for a V4L capture format."""
    width, height = resolution.lower().split('x')
    fourcc = _normalize_fourcc(v4l_format)
    media, gst_format = V4L_TO_GST.get(fourcc, ('video/x-raw', fourcc))

    if media == 'image/jpeg':
        caps = f'image/jpeg,width={width},height={height}'
        decoder = 'jpegdec' if check_gstreamer_element('jpegdec') else 'avdec_mjpeg'
        return [f'! {caps}', f'! {decoder}']

    if gst_format:
        caps = f'video/x-raw,format={gst_format},width={width},height={height}'
    else:
        caps = f'video/x-raw,width={width},height={height}'
    return [f'! {caps}']


def check_gstreamer_element(element_name):
    """Return True if a GStreamer element factory exists."""
    return Gst.ElementFactory.find(element_name) is not None


def discover_video_devices():
    """Return list of dicts: device, name, role, formats."""
    devices = []
    try:
        video_names = sorted(f for f in os.listdir('/dev') if f.startswith('video'))
    except OSError:
        return devices

    for name in video_names:
        device_path = f"/dev/{name}"
        if not os.path.exists(device_path):
            continue

        probed = probe_device_formats(device_path)
        role = device_role(probed) if probed else 'unknown'
        card = get_device_card_name(device_path)
        formats = get_device_formats(device_path)
        if formats:
            unique_formats = []
            seen = set()
            for fmt in formats:
                fmt_name = fmt.split(' (')[0] if ' (' in fmt else fmt
                if fmt_name not in seen:
                    seen.add(fmt_name)
                    unique_formats.append(fmt)

            if len(unique_formats) > 5:
                formats_str = ', '.join(unique_formats[:5]) + f" (+{len(unique_formats) - 5} more)"
            else:
                formats_str = ', '.join(unique_formats)
        else:
            formats_str = "(could not query - device may be in use)"

        devices.append({
            'device': device_path,
            'name': card,
            'role': role,
            'formats': formats_str,
        })

    return devices


def list_video_devices():
    """List available video devices with their supported output formats."""
    print("\nAvailable video devices and supported formats:")
    table_data = discover_video_devices()

    if not table_data:
        print("No video devices found.")
        return

    for i, device in enumerate(table_data, start=1):
        role = device.get('role') or 'unknown'
        name = device.get('name') or ''
        label = f"{device['device']}"
        if name:
            label += f"  ({name})"
        print(f"  [{i}] {label}  [{role}]  {device['formats']}")


def prompt_select_source():
    """Interactively select a V4L2 video source."""
    devices = discover_video_devices()
    if not devices:
        print("Error: No video devices found.")
        sys.exit(1)

    print("\nAvailable video sources:")
    for i, device in enumerate(devices, start=1):
        role = device.get('role') or 'unknown'
        name = device.get('name') or ''
        extra = f" ({name})" if name else ''
        print(f"  [{i}] {device['device']}{extra}  [{role}]  {device['formats']}")

    while True:
        try:
            choice = input(f"\nSelect source [1-{len(devices)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)

        if choice.isdigit() and 1 <= int(choice) <= len(devices):
            selected = devices[int(choice) - 1]
            print(f"Selected source: {selected['device']} [{selected.get('role', 'unknown')}]")
            if selected.get('role') == 'ir':
                print("Tip: IR nodes look wrong in color; for RealSense RGB use the [color] node (often /dev/video6).")
            elif selected.get('role') == 'depth':
                print("Tip: Depth (Z16) is not a normal color stream; pick a [color] node for RGB.")
            return selected['device']

        print(f"Invalid choice. Enter a number between 1 and {len(devices)}.")


def ensure_dest_list(path=DEST_LIST, default_path=DEST_LIST_DEFAULT):
    """Copy dest.list.default -> dest.list if the user list does not exist."""
    if os.path.isfile(path):
        return path
    if os.path.isfile(default_path):
        shutil.copy2(default_path, path)
        print(f"Created {path} from {os.path.basename(default_path)}")
        return path
    # Create an empty editable list so new targets can be saved
    with open(path, 'w', encoding='utf-8') as f:
        f.write("# Named destinations for video-publisher.py\n")
        f.write("# Format: label  url\n")
    print(f"Created empty {path}")
    return path


def load_dest_list(path=None):
    """Load destinations from dest.list. Returns list of {'label': str, 'url': str}."""
    if path is None:
        path = ensure_dest_list()
    else:
        ensure_dest_list(path)

    entries = []
    if not os.path.isfile(path):
        return entries

    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            # "label  url" or bare "url"
            if '://' in line:
                parts = line.split(None, 1)
                if len(parts) == 2 and '://' in parts[1]:
                    label, url = parts
                elif len(parts) == 1:
                    url = parts[0]
                    label = url
                else:
                    # label with spaces before url — take last token with ://
                    match = re.search(r'(\S+://\S+)$', line)
                    if not match:
                        continue
                    url = match.group(1)
                    label = line[:match.start()].strip() or url
            else:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    label, url = parts
                else:
                    label = url = parts[0]

            entries.append({'label': label, 'url': url})

    return entries


def _default_label_for_url(url):
    """Derive a short label from a destination URL."""
    try:
        parsed = urlparse(url if '://' in url else f'rtmp://{url}')
        path = (parsed.path or '').strip('/')
        if path:
            return path.replace('/', '-')
        if parsed.hostname:
            return parsed.hostname.replace('.', '-')
    except Exception:
        pass
    return 'dest'


def add_dest_entry(url, label=None, path=None):
    """Append a new destination to dest.list if the URL is not already present."""
    if path is None:
        path = ensure_dest_list()
    entries = load_dest_list(path)
    for entry in entries:
        if entry['url'] == url or entry['label'] == url:
            return False

    label = (label or _default_label_for_url(url)).strip() or _default_label_for_url(url)
    # Avoid duplicate labels by suffixing
    existing_labels = {e['label'] for e in entries}
    base = label
    n = 2
    while label in existing_labels:
        label = f'{base}-{n}'
        n += 1

    with open(path, 'a', encoding='utf-8') as f:
        f.write(f'{label}  {url}\n')
    print(f"Added destination [{label}] -> {url} to {os.path.basename(path)}")
    return True


def prompt_select_dest(dest_list_path=None):
    """Interactively select a destination from dest.list or enter a custom one."""
    if dest_list_path is None:
        dest_list_path = ensure_dest_list()
    else:
        ensure_dest_list(dest_list_path)

    entries = load_dest_list(dest_list_path)

    if entries:
        print("\nAvailable destinations:")
        for i, entry in enumerate(entries, start=1):
            if entry['label'] == entry['url']:
                print(f"  [{i}] {entry['url']}")
            else:
                print(f"  [{i}] {entry['label']}  ({entry['url']})")
        print("  [0] Enter custom destination")

        while True:
            try:
                choice = input(f"\nSelect destination [0-{len(entries)}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(1)

            if choice == '0':
                break
            if choice.isdigit() and 1 <= int(choice) <= len(entries):
                selected = entries[int(choice) - 1]['url']
                print(f"Selected destination: {selected}")
                return selected

            print(f"Invalid choice. Enter a number between 0 and {len(entries)}.")
    else:
        print(f"\nNo destinations found in {dest_list_path}")

    while True:
        try:
            custom = input("Enter destination (e.g. rtmp://host:1935/path): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)

        if not custom:
            print("Destination cannot be empty.")
            continue

        default_label = _default_label_for_url(custom)
        try:
            label = input(f"Label for this destination [{default_label}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if not label:
            label = default_label

        add_dest_entry(custom, label=label, path=dest_list_path)
        print(f"Selected destination: {custom}")
        return custom


def parse_destination(dest, default_protocol='rtmp', default_port=None, default_topic='/stream'):
    """
    Parse a destination string into protocol, host, port, topic.

    Accepts:
      rtmp://host:1935/stream/path
      udp://host:8000
      host:1935/stream/path
      host
    """
    protocol = default_protocol
    host = None
    port = default_port
    topic = default_topic

    if '://' in dest:
        parsed = urlparse(dest)
        if parsed.scheme:
            protocol = parsed.scheme.lower()
        host = parsed.hostname
        if parsed.port:
            port = parsed.port
        if parsed.path and parsed.path != '/':
            topic = parsed.path
    else:
        # host[:port][/path]
        match = re.match(r'^([^:/]+)(?::(\d+))?(/.*)?$', dest)
        if not match:
            raise ValueError(f"Invalid destination: {dest}")
        host = match.group(1)
        if match.group(2):
            port = int(match.group(2))
        if match.group(3):
            topic = match.group(3)

    if not host:
        raise ValueError(f"Could not parse host from destination: {dest}")

    if port is None:
        port = 1935 if protocol == 'rtmp' else 8000

    if not topic.startswith('/'):
        topic = f'/{topic}'

    return protocol, host, port, topic


def check_gstreamer_plugin(plugin_name):
    """Check if a GStreamer element (or plugin) is available."""
    if check_gstreamer_element(plugin_name):
        return True
    registry = Gst.Registry.get()
    return registry.find_plugin(plugin_name) is not None


def build_gstreamer_pipeline(device, server_ip, server_port, video_format=None, resolution='1280x720', topic='/stream', protocol='rtmp', rtmp_timeout=DEFAULT_RTMP_TIMEOUT):
    """Build GStreamer pipeline for UDP or RTMP streaming to MediaMTX."""

    v4l_format, resolution = detect_input_format(device, video_format, resolution)
    print(f"Input format: {v4l_format} @ {resolution}")

    if check_gstreamer_plugin('nvv4l2h264enc'):
        encoder = 'nvv4l2h264enc'
        video_convert = 'nvvidconv'
        print("Using Jetson hardware encoder (nvv4l2h264enc)")
    elif check_gstreamer_plugin('nv264enc'):
        encoder = 'nv264enc'
        video_convert = 'nvvidconv'
        print("Using NVIDIA GPU encoder (nv264enc)")
    elif check_gstreamer_plugin('vaapih264enc'):
        encoder = 'vaapih264enc'
        video_convert = 'vaapipostproc'
        print("Using VAAPI hardware encoder (vaapih264enc)")
    elif check_gstreamer_plugin('v4l2h264enc'):
        encoder = 'v4l2h264enc'
        video_convert = 'videoconvert'
        print("Using V4L2 hardware encoder (v4l2h264enc)")
    else:
        encoder = 'x264enc'
        video_convert = 'videoconvert'
        print("Using software encoder (x264enc)")

    pipeline_parts = [
        f'v4l2src device={device} do-timestamp=true',
    ]
    pipeline_parts.extend(build_source_caps(v4l_format, resolution))

    if video_convert == 'nvvidconv':
        pipeline_parts.append(f'! {video_convert}')
        pipeline_parts.append('! video/x-raw(memory:NVMM),format=NV12')
    elif video_convert == 'vaapipostproc':
        # Convert to NV12 in system memory first — avoids messy/corrupt color from
        # feeding UYVY/GREY straight into vaapipostproc on some Intel/UVC devices.
        pipeline_parts.append('! videoconvert ! video/x-raw,format=NV12')
        pipeline_parts.append('! vaapipostproc')
    else:
        pipeline_parts.append('! videoconvert')

    if encoder == 'nvv4l2h264enc':
        pipeline_parts.append(f'! {encoder} bitrate=2000000 iframeinterval=30 insert-sps-pps=true insert-vui=true')
    elif encoder == 'nv264enc':
        pipeline_parts.append(f'! {encoder} bitrate=2000000 insert-sps-pps=true')
    elif encoder == 'vaapih264enc':
        # bitrate is kbps for VAAPI; constrained-baseline + AUD for RTMP/HLS players
        pipeline_parts.append(
            f'! {encoder} rate-control=cbr bitrate=2000 keyframe-period=30 '
            f'max-bframes=0 aud=true quality-level=4 cpb-length=500'
        )
        pipeline_parts.append('! video/x-h264,profile=constrained-baseline')
    elif encoder == 'v4l2h264enc':
        pipeline_parts.append(f'! {encoder} keyframe-interval=30')
    else:
        pipeline_parts.append(f'! {encoder} bitrate=2000 speed-preset=ultrafast tune=zerolatency keyint=30 sync-lookahead=0 sliced-threads=true threads=1')

    pipeline_parts.append('! h264parse config-interval=-1')

    sink_location = None
    if protocol.lower() == 'rtmp':
        path = topic if topic.startswith('/') else f'/{topic}'
        rtmp_url = f'rtmp://{server_ip}:{server_port}{path}'
        # rtmp2sink requires app/stream path segments; a single segment like
        # rtmp://host/stream is corrupted to "rtmp:/" ("Host is not set").
        # Prefer librtmp rtmpsink, which accepts MediaMTX single-segment paths.
        if check_gstreamer_element('rtmpsink'):
            print("Using rtmpsink")
            pipeline_parts.extend([
                '! flvmux streamable=true',
                '! rtmpsink name=mysink sync=false'
            ])
            sink_location = rtmp_url
        else:
            print("Using rtmp2sink (adding trailing slash for single-segment paths)")
            if path.strip('/').count('/') == 0:
                rtmp_url = f'rtmp://{server_ip}:{server_port}{path}/'
            pipeline_parts.extend([
                '! flvmux streamable=true',
                f'! rtmp2sink name=mysink sync=false timeout={rtmp_timeout}'
            ])
            sink_location = rtmp_url
    else:
        pipeline_parts.extend([
            '! rtph264pay config-interval=1 pt=96 mtu=1400',
            f'! udpsink name=mysink host={server_ip} port={server_port} sync=false buffer-size=1048576'
        ])

    return ' '.join(pipeline_parts), sink_location


class Streamer:
    def __init__(self, pipeline_str, sink_location=None):
        self.pipeline_str = pipeline_str
        self.sink_location = sink_location
        self.pipeline = None
        self.sink = None
        self.last_bytes = 0
        self.last_time = time.time()
        self.stall_counter = 0
        self.bytes_out = 0
        self.loop = GLib.MainLoop()

    def on_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            print("\nEnd-of-stream")
            self.loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"\nPipeline Error: {err.message}")
            if debug:
                print(f"Debug Info: {debug}")
            self.loop.quit()
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                old_state, new_state, pending_state = message.parse_state_changed()
        return True

    def _on_sink_buffer(self, pad, info):
        buf = info.get_buffer()
        if buf:
            self.bytes_out += buf.get_size()
        return Gst.PadProbeReturn.OK

    def get_sink_bytes(self):
        """Bytes delivered to the sink pad (works for rtmpsink/udpsink/rtmp2sink)."""
        return self.bytes_out

    def status_timer_callback(self):
        if self.pipeline:
            _, state, _ = self.pipeline.get_state(0)
            state_name = Gst.Element.state_get_name(state)

            current_bytes = self.get_sink_bytes()
            current_time = time.time()

            duration = current_time - self.last_time
            if duration > 0:
                bitrate = (current_bytes - self.last_bytes) * 8 / (1024 * 1024) / duration

                status_msg = f"[{time.strftime('%H:%M:%S')}] Status: {state_name} | Bitrate: {bitrate:.2f} Mbps"

                if state == Gst.State.PLAYING and bitrate < 0.01:
                    self.stall_counter += 1
                    if self.stall_counter >= STALL_THRESHOLD:
                        print(f"\nStream stalled ({STALL_THRESHOLD}s no data). Forcing restart...")
                        self.loop.quit()
                        return False
                    status_msg += f" [STALLED {self.stall_counter}/{STALL_THRESHOLD}]"
                else:
                    self.stall_counter = 0

                print(f"\r{status_msg}\x1b[K", end='', flush=True)

                self.last_bytes = current_bytes
                self.last_time = current_time
        return True

    def run(self):
        try:
            self.pipeline = Gst.parse_launch(self.pipeline_str)
            self.sink = self.pipeline.get_by_name("mysink")
            if self.sink is not None and self.sink_location:
                self.sink.set_property('location', self.sink_location)
                print(f"RTMP location: {self.sink.get_property('location')}")
            # Track actual bytes into the sink (rtmpsink stats only expose rendered count)
            self.bytes_out = 0
            if self.sink is not None:
                sink_pad = self.sink.get_static_pad('sink')
                if sink_pad is not None:
                    sink_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_sink_buffer)
        except Exception as e:
            print(f"Failed to create pipeline: {e}")
            return False

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_message)

        print("Starting stream...")
        self.pipeline.set_state(Gst.State.PLAYING)

        self.last_bytes = 0
        self.last_time = time.time()
        self.stall_counter = 0

        GLib.timeout_add_seconds(1, self.status_timer_callback)

        try:
            self.loop.run()
        except KeyboardInterrupt:
            return False
        finally:
            print("\nSet final state NULL")
            self.pipeline.set_state(Gst.State.NULL)
        return True

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)


def main():
    parser = argparse.ArgumentParser(
        description='Stream video from V4L2 device to MediaMTX server via Python GStreamer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''\
Examples:
  %(prog)s                              # Interactive source + dest selection
  %(prog)s -s /dev/video0               # Source set, pick dest interactively
  %(prog)s -d rtmp://host:1935/live     # Dest set, pick source interactively
  %(prog)s -s /dev/video0 -d go2-front  # Non-interactive (label from dest.list)
'''
    )
    parser.add_argument('-l', '--list-devices', action='store_true', help='List available video devices and exit')
    parser.add_argument('-s', '--source', default=None, help='Video source device (e.g. /dev/video0)')
    parser.add_argument('-d', '--dest', default=None, help='Destination URL, host, or label from dest.list')
    parser.add_argument('--dest-list', default=DEST_LIST, help=f'Path to dest.list (default: {DEST_LIST}; seeded from dest.list.default)')
    parser.add_argument('-f', '--format', default=None, help='Force capture format (MJPG, YUYV, UYVY, ...); default: auto-detect')
    parser.add_argument('-r', '--resolution', default='1280x720', help='Resolution WIDTHxHEIGHT (default: 1280x720)')
    parser.add_argument('-t', '--topic', default='/stream', help='Default topic/path when dest has none')
    parser.add_argument('-p', '--port', type=int, default=None, help='Default port when dest has none (RTMP: 1935, UDP: 8000)')
    parser.add_argument('--protocol', choices=['udp', 'rtmp'], default='rtmp', help='Default protocol when dest has no scheme')
    parser.add_argument('--timeout', type=int, default=DEFAULT_RTMP_TIMEOUT, help=f'RTMP connection timeout (default: {DEFAULT_RTMP_TIMEOUT}s)')

    args = parser.parse_args()

    if args.list_devices:
        list_video_devices()
        sys.exit(0)

    ensure_dest_list(args.dest_list)

    # Resolve source
    source = args.source
    if not source:
        source = prompt_select_source()
    elif not os.path.exists(source):
        print(f"Error: Source device {source} not found")
        print("Use -l/--list-devices to see available devices")
        sys.exit(1)

    # Resolve destination
    dest = args.dest
    if not dest:
        dest = prompt_select_dest(args.dest_list)
    else:
        # Allow using a label from dest.list
        entries = load_dest_list(args.dest_list)
        matched = False
        for entry in entries:
            if entry['label'] == dest:
                dest = entry['url']
                matched = True
                break
        # New URL target via -d: persist it for next time
        if not matched and ('://' in dest or '.' in dest):
            add_dest_entry(dest, path=args.dest_list)

    try:
        protocol, server, port, topic = parse_destination(
            dest,
            default_protocol=args.protocol,
            default_port=args.port,
            default_topic=args.topic,
        )
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    pipeline_str, sink_location = build_gstreamer_pipeline(
        source, server, port,
        args.format, args.resolution, topic, protocol, args.timeout
    )

    print(f"\nSource: {source}")
    print(f"Dest:   {protocol}://{server}:{port}{topic}")
    print(f"\nPipeline: {pipeline_str}\n")

    streamer = Streamer(pipeline_str, sink_location=sink_location)

    retry_count = 0
    while True:
        should_retry = streamer.run()
        if not should_retry:
            print("\nStopped by user.")
            break

        retry_count += 1
        print(f"\nRestarting stream in {RECONNECT_DELAY}s... (attempt {retry_count})")
        time.sleep(RECONNECT_DELAY)


if __name__ == '__main__':
    main()
