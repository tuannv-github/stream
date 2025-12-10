# Video Streaming System

A complete video streaming solution using GStreamer and MediaMTX for publishing and subscribing to video streams. Supports hardware-accelerated encoding on Jetson, NVIDIA GPUs, and other platforms.

## Overview

This project provides tools for:
- **Publishing**: Stream video from V4L2 devices (cameras) to a MediaMTX server
- **Subscribing**: Receive and display video streams from MediaMTX
- **Hardware Acceleration**: Automatic detection and use of hardware encoders when available
- **Multiple Protocols**: Support for UDP RTP and RTMP streaming

## Directory Structure

```
stream/
├── publisher/          # Video publishing tools
│   ├── video-publisher.py      # Main video publisher script
│   ├── stream_publisher.py     # Alternative publisher
│   ├── stream_publisher_360.py # 360-degree video publisher
│   ├── check-hw-encoders.sh    # Hardware encoder detection
│   ├── install-gst.sh          # GStreamer installation script
│   └── stream_publisher.sh     # Shell script examples
├── subsciber/          # Video subscription tools
│   ├── stream_subscriber.py    # Main subscriber script
│   ├── gst-file.py             # File playback with PyQt5
│   ├── gst-360.py              # 360-degree video viewer
│   ├── gst-360-qt.py           # 360-degree video with Qt UI
│   ├── stream_subscriber.sh    # Shell script examples
│   └── ui/                     # UI components
└── mediamtx/           # MediaMTX server configuration
    ├── docker-compose.yml      # Docker Compose setup
    └── mediamtx.yaml           # MediaMTX configuration
```

## Prerequisites

- GStreamer 1.0+ with plugins
- Python 3.6+ (for Python scripts)
- Docker and Docker Compose (for MediaMTX server)
- V4L2-compatible video devices (for publishing)

### Installing GStreamer

Run the installation script:
```bash
cd publisher
./install-gst.sh
```

Or install manually:
```bash
# Ubuntu/Debian
sudo apt-get install gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav

# For Jetson (hardware acceleration)
sudo apt-get install gstreamer1.0-plugins-nv
```

## Quick Start

### 1. Start MediaMTX Server

```bash
cd mediamtx
docker-compose up -d
```

The server will be available at:
- RTSP: `rtsp://localhost:8554`
- RTMP: `rtmp://localhost:1935`
- HTTP: `http://localhost:8888`

### 2. Check Hardware Encoder Support

```bash
cd publisher
./check-hw-encoders.sh
```

This will detect available hardware encoders:
- Jetson: `nvv4l2h264enc`, `nvv4l2h265enc`
- NVIDIA GPU: `nv264enc`, `nvh264enc`
- Intel/AMD: `vaapih264enc`
- V4L2: `v4l2h264enc`
- Software fallback: `x264enc`

### 3. List Available Video Devices

```bash
python3 publisher/video-publisher.py -l
```

### 4. Publish a Video Stream

**Basic usage:**
```bash
python3 publisher/video-publisher.py
```

**Custom device and server:**
```bash
python3 publisher/video-publisher.py \
  -d /dev/video0 \
  -s 10.1.101.210 \
  -p 8000 \
  -f UYVY \
  -r 1920x1080 \
  -t /stream/go2/front
```

**RTMP streaming:**
```bash
python3 publisher/video-publisher.py \
  --protocol rtmp \
  -p 1935 \
  -t /live/stream
```

### 5. Subscribe to a Video Stream

**Using Python subscriber:**
```bash
python3 subsciber/stream_subscriber.py
```

**Using GStreamer directly:**
```bash
gst-launch-1.0 rtspsrc location=rtsp://10.1.101.210:8554/stream/go2/front \
  ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! autovideosink
```

## Publisher Options

The `video-publisher.py` script supports the following options:

| Option | Description | Default |
|--------|-------------|---------|
| `-l, --list-devices` | List available video devices | - |
| `-d, --device` | Video device path | `/dev/video4` |
| `-s, --server` | MediaMTX server IP | `10.1.101.210` |
| `-p, --port` | Server port (UDP: 8000, RTMP: 1935) | `8000` |
| `-f, --format` | Video format (UYVY, YUYV, MJPG) | `UYVY` |
| `-r, --resolution` | Resolution (WIDTHxHEIGHT) | `1280x720` |
| `-t, --topic` | Stream topic/path | `/stream/go2/front` |
| `--protocol` | Protocol: `udp` or `rtmp` | `udp` |

### Examples

**List devices:**
```bash
python3 publisher/video-publisher.py -l
```

**Stream from specific device:**
```bash
python3 publisher/video-publisher.py -d /dev/video1
```

**Custom resolution and format:**
```bash
python3 publisher/video-publisher.py -f YUYV -r 1920x1080
```

**RTMP streaming:**
```bash
python3 publisher/video-publisher.py --protocol rtmp -p 1935
```

## Hardware Acceleration

The publisher automatically detects and uses hardware encoders when available:

1. **Jetson/Tegra**: Uses `nvv4l2h264enc` with `nvvidconv`
2. **NVIDIA GPU**: Uses `nv264enc` or `nvh264enc`
3. **Intel/AMD**: Uses `vaapih264enc` with VAAPI
4. **V4L2**: Uses `v4l2h264enc`
5. **Software**: Falls back to `x264enc` with zero-latency tuning

### Low Latency Configuration

The pipeline is optimized for low latency:
- Minimal buffer sizes
- Zero-copy when possible (DMA mode)
- Hardware encoding with low-latency settings
- I-frame interval set to 1 for minimal delay
- `sync=false` for real-time streaming

## MediaMTX Configuration

The MediaMTX server is configured via `mediamtx/mediamtx.yaml`. Key settings:

- **UDP RTP**: Accepts streams on port 8000
- **RTMP**: Accepts streams on port 1935
- **RTSP**: Serves streams on port 8554
- **HTTP**: Serves HLS and other formats on port 8888

### Stream Paths

Streams are organized by topic/path:
- `/stream/go2/front` - Front camera stream
- `/stream/go2/back` - Back camera stream
- `/live/stream/360` - 360-degree video stream

## 360-Degree Video

For 360-degree video streaming:

**Publisher:**
```bash
python3 publisher/stream_publisher_360.py
# or
./subsciber/jetson_publish_360.sh
```

**Subscriber:**
```bash
python3 subsciber/gst-360.py
# or with Qt UI
python3 subsciber/gst-360-qt.py
```

## Troubleshooting

### No video devices found
- Check device permissions: `ls -l /dev/video*`
- Ensure device is not in use by another process
- Try listing devices: `python3 publisher/video-publisher.py -l`

### Hardware encoder not working
- Run `./publisher/check-hw-encoders.sh` to verify support
- Check device permissions: `ls -l /dev/nvhost-msenc` (Jetson)
- Ensure proper GStreamer plugins are installed

### Connection issues
- Verify MediaMTX server is running: `docker ps`
- Check firewall settings for ports 8000, 1935, 8554, 8888
- Verify network connectivity to server IP

### High latency
- Use hardware encoding when available
- Reduce resolution: `-r 640x480`
- Use UDP instead of RTMP: `--protocol udp`
- Check network conditions

## Advanced Usage

### Custom GStreamer Pipeline

You can modify the pipeline in `video-publisher.py` or use GStreamer directly:

```bash
gst-launch-1.0 v4l2src device=/dev/video0 \
  ! video/x-raw,format=UYVY,width=1280,height=720 \
  ! nvvidconv ! nvv4l2h264enc bitrate=2000000 \
  ! h264parse ! rtph264pay ! udpsink host=10.1.101.210 port=8000
```

### Recording Streams

To record a stream while viewing:
```bash
gst-launch-1.0 rtspsrc location=rtsp://10.1.101.210:8554/stream/go2/front \
  ! rtph264depay ! h264parse ! mp4mux ! filesink location=recording.mp4
```

## License

[Add your license information here]

## Contributing

[Add contribution guidelines here]
