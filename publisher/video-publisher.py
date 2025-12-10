#!/usr/bin/env python3
"""
Video Publisher - Stream video from V4L2 device to MediaMTX server via UDP
"""

import argparse
import subprocess
import sys
import os
import signal


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
                    # Format line like: [0]: 'YUYV' (YUYV 4:2:2)
                    # Save previous format's sizes before starting new format
                    if current_format and sizes:
                        if current_format not in sizes_by_format:
                            sizes_by_format[current_format] = []
                        sizes_by_format[current_format].extend(sizes)
                    
                    if "'" in line:
                        current_format = line.split("'")[1]
                        sizes = []
                elif line.startswith('Size:') and current_format:
                    # Size line like: Size: Discrete 640x480
                    size_info = line.replace('Size: Discrete ', '').replace('Size: Stepwise ', '').strip()
                    if size_info and 'x' in size_info:
                        sizes.append(size_info)
                elif line.startswith('Interval:') and current_format and sizes:
                    # Interval line - store sizes for current format
                    if current_format not in sizes_by_format:
                        sizes_by_format[current_format] = []
                    sizes_by_format[current_format].extend(sizes)
                    sizes = []
            
            # Save last format's sizes if any
            if current_format and sizes:
                if current_format not in sizes_by_format:
                    sizes_by_format[current_format] = []
                sizes_by_format[current_format].extend(sizes)
            
            # Add formats with their sizes
            for fmt, size_list in sizes_by_format.items():
                unique_sizes = sorted(set(size_list))[:5]  # Show up to 5 sizes
                if unique_sizes:
                    formats.append(f"{fmt} ({', '.join(unique_sizes)})")
                else:
                    formats.append(fmt)
        
        # If no detailed formats, try simple format listing
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
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    
    return formats


def list_video_devices():
    """List available video devices with their supported output formats."""
    print("\nAvailable video devices and supported formats:")
    
    devices_found = []
    
    # List only /dev/video* devices
    for dev in sorted([f for f in os.listdir('/dev') if f.startswith('video')]):
        dev_path = f"/dev/{dev}"
        if os.path.exists(dev_path):
            devices_found.append({'name': dev_path, 'path': dev_path})
    
    # Collect all device data for table display
    table_data = []
    for device_info in devices_found:
        device_path = device_info.get('path') or device_info.get('name', '')
        
        # Get supported formats
        formats = get_device_formats(device_path)
        if formats:
            # Show unique formats (remove duplicates)
            unique_formats = []
            seen = set()
            for fmt in formats:
                # Extract just the format name for deduplication
                fmt_name = fmt.split(' (')[0] if ' (' in fmt else fmt
                if fmt_name not in seen:
                    seen.add(fmt_name)
                    unique_formats.append(fmt)
            
            # Format formats string (limit to first 5 for table)
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
    
    # Print table
    if not table_data:
        print("No video devices found.")
        return
    
    # Calculate column widths
    max_device_len = max(len(d['device']) for d in table_data) if table_data else 0
    max_formats_len = max(len(d['formats']) for d in table_data) if table_data else 0
    
    # Set minimum column widths
    device_width = max(20, max_device_len + 2)
    formats_width = max(50, max_formats_len + 2)
    
    # Print table header
    print(f"\n{'Device':<{device_width}} {'Supported Formats':<{formats_width}}")
    print("=" * (device_width + formats_width))
    
    # Print table rows
    for device in table_data:
        dev = device['device'][:device_width-2] if len(device['device']) > device_width-2 else device['device']
        formats = device['formats'][:formats_width-2] if len(device['formats']) > formats_width-2 else device['formats']
        print(f"{dev:<{device_width}} {formats:<{formats_width}}")


def check_gstreamer_plugin(plugin_name):
    """Check if a GStreamer plugin is available."""
    try:
        result = subprocess.run(
            ['gst-inspect-1.0', plugin_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def build_gstreamer_pipeline(device, server_ip, server_port, video_format='UYVY', resolution='1920x1080', topic='/stream/go2/front', protocol='udp'):
    """Build GStreamer pipeline for UDP or RTMP streaming to MediaMTX."""
    
    # Parse resolution
    try:
        width, height = resolution.split('x')
        width = int(width)
        height = int(height)
    except ValueError:
        print(f"Warning: Invalid resolution format '{resolution}', using default 1920x1080")
        width, height = 1920, 1080
    
    # Check for hardware encoders (Jetson/Tegra)
    use_hw_encoder = False
    encoder = None
    video_convert = None
    
    if check_gstreamer_plugin('nvv4l2h264enc'):
        # Jetson hardware encoder
        use_hw_encoder = True
        encoder = 'nvv4l2h264enc'
        video_convert = 'nvvidconv'
        print("Using Jetson hardware encoder (nvv4l2h264enc)")
    elif check_gstreamer_plugin('nv264enc'):
        # NVIDIA desktop GPU encoder
        use_hw_encoder = True
        encoder = 'nv264enc'
        video_convert = 'nvvidconv'
        print("Using NVIDIA GPU encoder (nv264enc)")
    elif check_gstreamer_plugin('vaapih264enc'):
        # Intel/AMD VAAPI encoder
        use_hw_encoder = True
        encoder = 'vaapih264enc'
        video_convert = 'vaapipostproc'
        print("Using VAAPI hardware encoder (vaapih264enc)")
    elif check_gstreamer_plugin('v4l2h264enc'):
        # V4L2 hardware encoder
        use_hw_encoder = True
        encoder = 'v4l2h264enc'
        video_convert = 'videoconvert'
        print("Using V4L2 hardware encoder (v4l2h264enc)")
    else:
        # Software encoder fallback
        encoder = 'x264enc'
        video_convert = 'videoconvert'
        print("Using software encoder (x264enc)")
    
    # Build pipeline components
    # Start with v4l2src - optimized for low latency
    # io-mode=2 (DMA) for zero-copy when possible, do-timestamp=true for accurate timing
    pipeline_parts = [
        f'v4l2src device={device} io-mode=2 do-timestamp=true',
        '! video/x-raw'
    ]
    
    # Add video conversion based on encoder type - only convert if absolutely necessary
    # Goal: minimize conversions for low latency
    if video_convert == 'nvvidconv':
        # For Jetson: nvvidconv can accept I420, NV12, YUY2, UYVY directly
        # It converts to NVMM format internally - no pre-conversion needed
        # This avoids an extra videoconvert step, reducing latency
        pipeline_parts.append(f'! {video_convert}')
        pipeline_parts.append('! video/x-raw(memory:NVMM),format=NV12')
    elif video_convert == 'vaapipostproc':
        # For VAAPI: vaapipostproc handles format conversion internally
        # Let it negotiate from native format
        pipeline_parts.append(f'! {video_convert}')
        pipeline_parts.append('! video/x-raw,format=NV12')
    elif video_convert == 'videoconvert':
        # For software/V4L2 encoder: let encoder negotiate format directly
        # Most encoders can handle common formats (I420, NV12, YUY2, UYVY)
        # Only convert if encoder absolutely requires a specific format
        # For now, let it pass through and let encoder negotiate
        pipeline_parts.append('! video/x-raw')
    else:
        # Fallback: let format negotiate automatically
        pipeline_parts.append('! video/x-raw')
    
    # Add encoder with low-latency optimizations
    if encoder == 'nvv4l2h264enc':
        # Jetson hardware encoder - optimize for lowest latency
        # Use only valid properties: bitrate, iframeinterval (for I-frame frequency)
        pipeline_parts.append(f'! {encoder} bitrate=2000000 iframeinterval=1')
    elif encoder == 'nv264enc':
        # NVIDIA GPU encoder - use valid properties only
        pipeline_parts.append(f'! {encoder} bitrate=2000000')
    elif encoder == 'vaapih264enc':
        # VAAPI encoder - low latency tuning
        pipeline_parts.append(f'! {encoder} bitrate=2000000 tune=low-latency keyframe-period=1')
    elif encoder == 'v4l2h264enc':
        # V4L2 hardware encoder - minimal settings for low latency
        pipeline_parts.append(f'! {encoder} keyframe-interval=1')
    else:
        # x264enc software encoder - maximum low-latency settings
        pipeline_parts.append(f'! {encoder} bitrate=2000 speed-preset=ultrafast tune=zerolatency keyint=1 sync-lookahead=0 sliced-threads=true threads=1')
    
    # Add H.264 parsing - minimal processing for low latency
    # Note: Some encoders output proper NAL units and h264parse can be skipped
    # But rtph264pay typically needs it - keeping it minimal
    pipeline_parts.append('! h264parse')
    
    if protocol.lower() == 'rtmp':
        # RTMP streaming: use FLV muxer and RTMP sink
        # RTMP URL format: rtmp://server:port/topic
        # Optimize for low latency
        rtmp_url = f'rtmp://{server_ip}:{server_port}{topic}'
        pipeline_parts.extend([
            '! flvmux streamable=true',
            f'! rtmpsink location={rtmp_url} sync=false'
        ])
    else:
        # UDP RTP streaming (default) - optimized for lowest latency
        # Topic is used for MediaMTX server configuration - ensure MediaMTX is configured
        # to accept UDP RTP streams on this topic/path from the source IP/port
        pipeline_parts.extend([
            '! rtph264pay config-interval=1 pt=96 mtu=1400',  # Smaller MTU for lower latency
            f'! udpsink host={server_ip} port={server_port} sync=false buffer-size=1'  # sync=false and minimal buffer
        ])
    
    pipeline = ' '.join(pipeline_parts)
    return pipeline


def main():
    parser = argparse.ArgumentParser(
        description='Stream video from V4L2 device to MediaMTX server via UDP or RTMP',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -l                    # List available video devices
  %(prog)s                       # Stream from /dev/video4 with UYVY@1280x720 to /stream/go2/front (RTMP)
  %(prog)s -d /dev/video1        # Stream from /dev/video1
  %(prog)s -s 192.168.1.100 -p 1935  # Stream to custom server
  %(prog)s -f YUYV -r 1280x720   # Use YUYV format at 1280x720 resolution
  %(prog)s -t /stream/go2/back   # Stream to different topic
  %(prog)s --protocol udp -p 8000  # Stream via UDP on port 8000
        """
    )
    
    parser.add_argument(
        '-l', '--list-devices',
        action='store_true',
        help='List available video devices and exit'
    )
    
    parser.add_argument(
        '-d', '--device',
        default='/dev/video4',
        help='Video device path (default: /dev/video4)'
    )
    
    parser.add_argument(
        '-s', '--server',
        default='10.1.101.210',
        help='MediaMTX server IP address (default: 10.1.101.210)'
    )
    
    parser.add_argument(
        '-p', '--port',
        type=int,
        default=1935,
        help='MediaMTX server port (UDP: 8000, RTMP: 1935) (default: 1935)'
    )
    
    parser.add_argument(
        '-f', '--format',
        default='UYVY',
        help='Video format (e.g., UYVY, YUYV, MJPG) (default: UYVY)'
    )
    
    parser.add_argument(
        '-r', '--resolution',
        default='1280x720',
        help='Video resolution in WIDTHxHEIGHT format (default: 1280x720)'
    )
    
    parser.add_argument(
        '-t', '--topic',
        default='/stream/go2/front',
        help='MediaMTX stream topic/path (default: /stream/go2/front)'
    )
    
    parser.add_argument(
        '--protocol',
        choices=['udp', 'rtmp'],
        default='rtmp',
        help='Streaming protocol: udp (UDP RTP) or rtmp (RTMP) (default: rtmp)'
    )
    
    args = parser.parse_args()
    
    # List devices and exit if requested
    if args.list_devices:
        list_video_devices()
        sys.exit(0)
    
    # Check if device exists
    if not os.path.exists(args.device):
        print(f"Error: Video device {args.device} not found")
        print("\nUse -l or --list-devices to see available devices")
        sys.exit(1)
    
    # Check if GStreamer is available
    try:
        subprocess.run(['gst-inspect-1.0', '--version'], 
                      capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("Error: GStreamer not found. Please install GStreamer first.")
        print("Run: ./install-gst.sh")
        sys.exit(1)
    
    # Adjust port based on protocol if user specified wrong default
    if args.protocol == 'rtmp' and args.port == 8000:
        # RTMP default port is 1935
        args.port = 1935
    elif args.protocol == 'udp' and args.port == 1935:
        # UDP default port is 8000
        args.port = 8000
    
    # Build and print pipeline
    pipeline = build_gstreamer_pipeline(
        args.device, 
        args.server, 
        args.port,
        args.format,
        args.resolution,
        args.topic,
        args.protocol
    )
    
    print(f"\nStreaming from {args.device} to {args.server}:{args.port}")
    print(f"Protocol: {args.protocol.upper()}")
    print(f"Topic/Path: {args.topic}")
    print(f"Format: {args.format}, Resolution: {args.resolution}")
    
    if args.protocol == 'rtmp':
        print(f"\nNote: RTMP stream URL: rtmp://{args.server}:{args.port}{args.topic}")
        print(f"      Ensure MediaMTX server is configured to accept RTMP streams on this path")
    else:
        print(f"\nNote: Ensure MediaMTX server is configured to accept UDP RTP stream on topic '{args.topic}'")
        print(f"      from source {args.server}:{args.port}")
    print(f"\nPipeline: {pipeline}\n")
    
    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        print("\nStopping stream...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run GStreamer pipeline
    try:
        # gst-launch-1.0 expects the pipeline as a single string
        cmd = ['gst-launch-1.0'] + pipeline.split()
        print(f"Running: gst-launch-1.0 {pipeline}\n")
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        print("\nStopping stream...")
        sys.exit(0)
    except subprocess.CalledProcessError as e:
        print(f"\nError running GStreamer pipeline: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()

