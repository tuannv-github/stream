#!/usr/bin/env python3
"""
Video File Publisher - Stream video file to MediaMTX server via UDP or RTMP
"""

import argparse
import subprocess
import sys
import os
import signal


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


def build_gstreamer_pipeline(video_file, server_ip, server_port, topic='/stream/go2/front', protocol='udp', loop=False):
    """Build GStreamer pipeline for streaming video file to MediaMTX."""
    
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
    # Start with filesrc to read the video file
    pipeline_parts = [
        f'filesrc location={video_file}'
    ]
    
    # Note: filesrc doesn't support loop property
    # Looping is handled at the application level
    
    # Use decodebin to automatically decode the video file
    # This handles various video formats (MP4, AVI, MKV, etc.)
    pipeline_parts.append('! decodebin')
    
    # Add video conversion based on encoder type
    if video_convert == 'nvvidconv':
        # For Jetson: nvvidconv converts to NVMM format
        pipeline_parts.append(f'! {video_convert}')
        pipeline_parts.append('! video/x-raw(memory:NVMM),format=NV12')
    elif video_convert == 'vaapipostproc':
        # For VAAPI: vaapipostproc handles format conversion
        pipeline_parts.append(f'! {video_convert}')
        pipeline_parts.append('! video/x-raw,format=NV12')
    elif video_convert == 'videoconvert':
        # For software/V4L2 encoder: convert to a format the encoder can handle
        pipeline_parts.append('! videoconvert')
        pipeline_parts.append('! video/x-raw,format=I420')
    else:
        # Fallback: let format negotiate automatically
        pipeline_parts.append('! videoconvert')
        pipeline_parts.append('! video/x-raw')
    
    # Add encoder with low-latency optimizations
    if encoder == 'nvv4l2h264enc':
        # Jetson hardware encoder
        pipeline_parts.append(f'! {encoder} iframeinterval=1')
    elif encoder == 'nv264enc':
        # NVIDIA GPU encoder
        pipeline_parts.append(f'! {encoder}')
    elif encoder == 'vaapih264enc':
        # VAAPI encoder - low latency tuning
        pipeline_parts.append(f'! {encoder} tune=low-latency keyframe-period=1')
    elif encoder == 'v4l2h264enc':
        # V4L2 hardware encoder
        pipeline_parts.append(f'! {encoder} keyframe-interval=1')
    else:
        # x264enc software encoder - low-latency settings
        # pipeline_parts.append(f'! {encoder} speed-preset=ultrafast tune=zerolatency key-int-max=1 sync-lookahead=0 sliced-threads=true threads=1')
        pipeline_parts.append(f'! {encoder} ')
    
    # Add H.264 parsing
    pipeline_parts.append('! h264parse')
    
    if protocol.lower() == 'rtmp':
        # RTMP streaming: use FLV muxer and RTMP sink
        rtmp_url = f'rtmp://{server_ip}:{server_port}{topic}'
        pipeline_parts.extend([
            '! flvmux streamable=true',
            f'! rtmpsink location={rtmp_url} sync=true'
        ])
    else:
        # UDP RTP streaming (default) - optimized for lowest latency
        pipeline_parts.extend([
            '! rtph264pay config-interval=1 pt=96 mtu=1400',
            f'! udpsink host={server_ip} port={server_port} sync=false buffer-size=1'
        ])
    
    pipeline = ' '.join(pipeline_parts)
    return pipeline


def main():
    parser = argparse.ArgumentParser(
        description='Stream video file to MediaMTX server via UDP or RTMP',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s video.mp4                    # Stream video file to /stream/go2/front (UDP)
  %(prog)s video.mp4 -t /stream/go2/back # Stream to different topic
  %(prog)s video.mp4 --protocol rtmp    # Stream via RTMP
  %(prog)s video.mp4 --loop             # Loop the video file
  %(prog)s video.mp4 -s 192.168.1.100   # Stream to custom server
        """
    )
    
    parser.add_argument(
        'video_file',
        help='Path to video file to stream'
    )
    
    parser.add_argument(
        '-s', '--server',
        default='10.1.101.210',
        help='MediaMTX server IP address (default: 10.1.101.210)'
    )
    
    parser.add_argument(
        '-p', '--port',
        type=int,
        default=8000,
        help='MediaMTX server port (UDP: 8000, RTMP: 1935) (default: 8000)'
    )
    
    parser.add_argument(
        '-t', '--topic',
        default='/stream/go2/front',
        help='MediaMTX stream topic/path (default: /stream/go2/front)'
    )
    
    parser.add_argument(
        '--protocol',
        choices=['udp', 'rtmp'],
        default='udp',
        help='Streaming protocol: udp (UDP RTP) or rtmp (RTMP) (default: udp)'
    )
    
    parser.add_argument(
        '--loop',
        action='store_true',
        help='Loop the video file when it reaches the end'
    )
    
    args = parser.parse_args()
    
    # Check if video file exists
    if not os.path.exists(args.video_file):
        print(f"Error: Video file {args.video_file} not found")
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
        args.video_file,
        args.server,
        args.port,
        args.topic,
        args.protocol,
        args.loop
    )
    
    print(f"\nStreaming {args.video_file} to {args.server}:{args.port}")
    print(f"Protocol: {args.protocol.upper()}")
    print(f"Topic/Path: {args.topic}")
    if args.loop:
        print("Loop: Enabled")
    
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
    cmd = ['gst-launch-1.0'] + pipeline.split()
    print(f"Running: gst-launch-1.0 {pipeline}\n")
    
    if args.loop:
        # Loop forever by restarting the pipeline when it ends
        iteration = 0
        while True:
            try:
                iteration += 1
                if iteration > 1:
                    print(f"\nRestarting stream (iteration {iteration})...\n")
                result = subprocess.run(cmd, check=False)
                # If pipeline ended successfully (EOS), restart it
                if result.returncode == 0:
                    print("\nVideo ended, restarting...")
                    continue
                else:
                    # Pipeline ended with error, exit
                    print(f"\nError running GStreamer pipeline: {result}")
                    sys.exit(1)
            except KeyboardInterrupt:
                print("\nStopping stream...")
                sys.exit(0)
            except Exception as e:
                print(f"\nUnexpected error: {e}")
                sys.exit(1)
    else:
        # Run once
        try:
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
