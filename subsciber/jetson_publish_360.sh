# Wake theta up
theta --set-property=0xD80E --val=0x00;
# Put to live stream mode
theta --set-property=0x5013 --val=0x8005;

gst-launch-1.0 -q thetauvcsrc mode=1 ! decodebin ! nvvidconv qos=true flip-method=2 ! nvvidconv  ! clockoverlay time-format="%Y-%m-%d %H:%M:%S in SG" valignment=top halignment=right font-desc="Sans, 20" ! nvvidconv ! nvv4l2h264enc bitrate=1000 iframeinterval=30 idrinterval=1 maxperf-enable=true !  h264parse ! flvmux name=mux streamable=false ! rtmpsink location='rtmp://10.10.0.11:1935/live/stream/360' sync=false async=false qos=true
