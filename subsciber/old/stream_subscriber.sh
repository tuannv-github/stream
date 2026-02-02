# gst-launch-1.0 -v rtmpsrc location="rtmp://10.10.0.11/live/stream/hd" \
#   ! flvdemux \
#   ! decodebin \
#   ! videoconvert \
#   ! autovideosink

gst-launch-1.0 rtspsrc location=rtsp://10.10.0.11:8554/live/stream/360 ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! autovideosink sync=false
