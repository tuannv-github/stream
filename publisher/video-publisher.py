#!/usr/bin/env python3
"""
Video Publisher - Stream video from V4L2 device to MediaMTX server via GStreamer Python bindings
"""

import argparse
import subprocess
import sys
import os
import signal
import time
import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# Initialize GStreamer
Gst.init(None)

# Constants
RECONNECT_DELAY = 2
STALL_THRESHOLD = 5
DEFAULT_RTMP_TIMEOUT = 2


def get_device_formats(device):
    """Get supported formats for a video device using v4l2-ctl."""
    formats = []
    sizes_by_format = {}
    
    try:
        # Try detailed format listing first
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
                        if current_format not in sizes_by_format:
                            sizes_by_format[current_format] = []
                        sizes_by_format[current_format].extend(sizes)
                    
                    if "'" in line:
                        current_format = line.split("'")[1]
                        sizes = []
                elif line.startswith('Size:') and current_format:
                    size_info = line.replace('Size: Discrete ', '').replace('Size: Stepwise ', '').strip()
                    if size_info and 'x' in size_info:
                        sizes.append(size_info)
                elif line.startswith('Interval:') and current_format and sizes:
                    if current_format not in sizes_by_format:
                        sizes_by_format[current_format] = []
                    sizes_by_format[current_format].extend(sizes)
                    sizes = []
            
            if current_format and sizes:
                if current_format not in sizes_by_format:
                    sizes_by_format[current_format] = []
                sizes_by_format[current_format].extend(sizes)
            
            for fmt, size_list in sizes_by_format.items():
                unique_sizes = sorted(set(size_list))[:5]
                if unique_sizes:
                    formats.append(f"{fmt} ({', '.join(unique_sizes)})")
                else:
                    formats.append(fmt)
        
        if not formats:
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
                        fmt = line.split("'")[1]
                        if fmt not in formats:
                            formats.append(fmt)
    except Exception:
        pass
    
    return formats


def list_video_devices():
    """List available video devices with their supported output formats."""
    print("\nAvailable video devices and supported formats:")
    
    devices_found = []
    for dev in sorted([f for f in os.listdir('/dev') if f.startswith('video')]):
        dev_path = f"/dev/{dev}"
        if os.path.exists(dev_path):
            devices_found.append({'name': dev_path, 'path': dev_path})
    
    table_data = []
    for device_info in devices_found:
        device_path = device_info.get('path') or device_info.get('name', '')
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
        
        table_data.append({
            'device': device_path,
            'formats': formats_str
        })
    
    if not table_data:
        print("No video devices found.")
        return
    
    max_device_len = max(len(d['device']) for d in table_data)
    max_formats_len = max(len(d['formats']) for d in table_data)
    device_width = max(20, max_device_len + 2)
    formats_width = max(50, max_formats_len + 2)
    
    print(f"\n{'Device':<{device_width}} {'Supported Formats':<{formats_width}}")
    print("=" * (device_width + formats_width))
    for device in table_data:
        dev = device['device'][:device_width-2]
        formats = device['formats'][:formats_width-2]
        print(f"{dev:<{device_width}} {formats:<{formats_width}}")


def check_gstreamer_plugin(plugin_name):
    """Check if a GStreamer plugin is available using Gst Registry."""
    registry = Gst.Registry.get()
    return registry.find_plugin(plugin_name) is not None or \
           registry.find_feature(plugin_name, Gst.ElementFactory.__gtype__) is not None


def build_gstreamer_pipeline(device, server_ip, server_port, video_format='UYVY', resolution='1920x1080', topic='/stream/go2/front', protocol='rtmp', rtmp_timeout=DEFAULT_RTMP_TIMEOUT):
    """Build GStreamer pipeline for UDP or RTMP streaming to MediaMTX."""
    
    # Check for hardware encoders
    encoder = None
    video_convert = None
    
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
        f'v4l2src device={device} io-mode=2 do-timestamp=true',
        '! video/x-raw'
    ]
    
    if video_convert == 'nvvidconv':
        pipeline_parts.append(f'! {video_convert}')
        pipeline_parts.append('! video/x-raw(memory:NVMM),format=NV12')
    elif video_convert == 'vaapipostproc':
        pipeline_parts.append(f'! {video_convert}')
        pipeline_parts.append('! video/x-raw,format=NV12')
    else:
        pipeline_parts.append('! videoconvert')
    
    if encoder == 'nvv4l2h264enc':
        # Jetson hardware encoder - optimize for lowest latency
        # Added insert-sps-pps and insert-vui for better client playability
        pipeline_parts.append(f'! {encoder} bitrate=2000000 iframeinterval=30 insert-sps-pps=true insert-vui=true')
    elif encoder == 'nv264enc':
        # NVIDIA GPU encoder
        pipeline_parts.append(f'! {encoder} bitrate=2000000 insert-sps-pps=true')
    elif encoder == 'vaapih264enc':
        # VAAPI encoder
        pipeline_parts.append(f'! {encoder} bitrate=2000000 tune=low-latency keyframe-period=30')
    elif encoder == 'v4l2h264enc':
        # V4L2 hardware encoder
        pipeline_parts.append(f'! {encoder} keyframe-interval=30')
    else:
        # x264enc software encoder
        pipeline_parts.append(f'! {encoder} bitrate=2000 speed-preset=ultrafast tune=zerolatency keyint=30 sync-lookahead=0 sliced-threads=true threads=1')
    
    # Add H.264 parsing - send config interval for better client discovery
    pipeline_parts.append('! h264parse config-interval=1')
    
    if protocol.lower() == 'rtmp':
        # RTMP streaming: Use rtmp2sink (preferred) with timeout
        path = topic if topic.startswith('/') else f'/{topic}'
        rtmp_url = f'rtmp://{server_ip}:{server_port}{path}'
        # Using rtmp2sink for better reliability and built-in timeout support
        pipeline_parts.extend([
            '! flvmux streamable=true',
            f'! rtmp2sink name=mysink location="{rtmp_url}" sync=false timeout={rtmp_timeout}'
        ])
    else:
        # UDP RTP streaming (default)
        pipeline_parts.extend([
            '! rtph264pay config-interval=1 pt=96 mtu=1400',
            f'! udpsink name=mysink host={server_ip} port={server_port} sync=false buffer-size=1048576'
        ])
    
    return ' '.join(pipeline_parts)


class Streamer:
    def __init__(self, pipeline_str):
        self.pipeline_str = pipeline_str
        self.pipeline = None
        self.sink = None
        self.last_bytes = 0
        self.last_time = time.time()
        self.stall_counter = 0
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
                # print(f"Pipeline state changed from {old_state.value_name} to {new_state.value_name}")
        return True

    def get_sink_bytes(self):
        if not self.sink:
            return 0
        
        # Check if it's udpsink or rtmp2sink
        try:
            if hasattr(self.sink.props, 'bytes_served'):
                return self.sink.get_property('bytes-served')
            elif self.sink.get_factory().get_name() == 'rtmp2sink':
                stats = self.sink.get_property('stats')
                if stats and stats.has_field('out-bytes-total'):
                    return stats.get_uint64('out-bytes-total')[1]
        except Exception:
            pass
        return 0

    def status_timer_callback(self):
        if self.pipeline:
            _, state, _ = self.pipeline.get_state(0)
            state_name = Gst.Element.state_get_name(state)
            
            current_bytes = self.get_sink_bytes()
            current_time = time.time()
            
            duration = current_time - self.last_time
            if duration > 0:
                bitrate = (current_bytes - self.last_bytes) * 8 / (1024 * 1024) / duration  # Mbps
                
                status_msg = f"[{time.strftime('%H:%M:%S')}] Status: {state_name} | Bitrate: {bitrate:.2f} Mbps"
                
                # Simple stall detection: if we are playing but bitrate is 0 for a while
                if state == Gst.State.PLAYING and bitrate < 0.01:
                    self.stall_counter += 1
                    if self.stall_counter >= STALL_THRESHOLD:
                        print(f"\nStream stalled ({STALL_THRESHOLD}s no data). Forcing restart...")
                        self.loop.quit()
                        return False 
                    status_msg += f" [STALLED {self.stall_counter}/{STALL_THRESHOLD}]"
                else:
                    self.stall_counter = 0
                
                # Use \x1b[K to clear to the end of the line
                print(f"\r{status_msg}\x1b[K", end='', flush=True)
                
                self.last_bytes = current_bytes
                self.last_time = current_time
        return True

    def run(self):
        try:
            self.pipeline = Gst.parse_launch(self.pipeline_str)
            self.sink = self.pipeline.get_by_name("mysink")
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
        
        # Add a timer to print status every second
        GLib.timeout_add_seconds(1, self.status_timer_callback)
        
        try:
            self.loop.run()
        except KeyboardInterrupt:
            return False # Signal to stop completely
        finally:
            print("Set final state NULL")
            self.pipeline.set_state(Gst.State.NULL)
        return True # Signal to retry

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

def main():
    parser = argparse.ArgumentParser(description='Stream video from V4L2 device to MediaMTX server via Python GStreamer')
    parser.add_argument('-l', '--list-devices', action='store_true', help='List available video devices')
    parser.add_argument('-d', '--device', default='/dev/video4', help='Video device path')
    parser.add_argument('-s', '--server', default='129.126.114.218', help='MediaMTX server IP')
    parser.add_argument('-p', '--port', type=int, default=1935, help='Port (UDP: 8000, RTMP: 1935)')
    parser.add_argument('-f', '--format', default='UYVY', help='Video format')
    parser.add_argument('-r', '--resolution', default='1280x720', help='Resolution WIDTHxHEIGHT')
    parser.add_argument('-t', '--topic', default='/stream/go2/front', help='Topic/Path')
    parser.add_argument('--protocol', choices=['udp', 'rtmp'], default='rtmp', help='Protocol')
    parser.add_argument('--timeout', type=int, default=DEFAULT_RTMP_TIMEOUT, help=f'RTMP connection timeout (default: {DEFAULT_RTMP_TIMEOUT}s)')
    
    args = parser.parse_args()
    
    if args.list_devices:
        list_video_devices()
        sys.exit(0)
    
    if not os.path.exists(args.device):
        print(f"Error: Device {args.device} not found")
        sys.exit(1)
    
    if args.protocol == 'rtmp' and args.port == 8000:
        args.port = 1935
    elif args.protocol == 'udp' and args.port == 1935:
        args.port = 8000
    
    pipeline_str = build_gstreamer_pipeline(
        args.device, args.server, args.port,
        args.format, args.resolution, args.topic, args.protocol, args.timeout
    )
    
    print(f"\nPipeline: {pipeline_str}\n")
    
    streamer = Streamer(pipeline_str)
    
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
