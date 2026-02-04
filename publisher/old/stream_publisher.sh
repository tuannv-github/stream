#!/bin/bash

# gst-launch-1.0 -v v4l2src device=/dev/video0 ! videoconvert ! x264enc ! flvmux ! rtmpsink location="rtmp://10.10.0.11/live/stream"

# gst-launch-1.0 -v v4l2src device=/dev/video0 ! video/x-raw,format=YUY2,width=640,height=480,framerate=30/1 ! \
#     videoconvert ! \
#     timeoverlay halignment=left valignment=top font-desc="Sans, 24" shaded-background=true ! \
#     x264enc tune=zerolatency bitrate=1500 speed-preset=superfast ! flvmux ! \
#     rtmpsink location="rtmp://10.10.0.11/live/stream"

gst-launch-1.0 -v v4l2src device=/dev/video0 ! video/x-raw,format=YUY2,width=640,height=480,framerate=30/1 ! \
    videoconvert ! \
    timeoverlay time-format="%Y-%m-%d %H:%M:%S.%3f" halignment=left valignment=top font-desc="Sans, 24" shaded-background=true ! \
    x264enc tune=zerolatency bitrate=1500 speed-preset=superfast ! flvmux ! \
    rtmpsink location="rtmp://10.10.0.11/live/stream"


