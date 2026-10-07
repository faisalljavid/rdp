"""
GStreamer PipeWire Video Capture & JPEG Streaming Pipeline.
"""

import sys
from typing import Callable, Optional

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)


class VideoPipeline:
    """
    GStreamer pipeline capturing from PipeWire (using portal fd and node id),
    converting and encoding frames to JPEG, and delivering them via appsink.
    """

    def __init__(
        self,
        fd: int,
        node_id: int,
        stream_width: int,
        stream_height: int,
        fps: int = 30,
        quality: int = 70,
        scale: float = 1.0,
        is_mock: bool = False,
        on_frame: Optional[Callable[[bytes], None]] = None,
        on_error_or_eos: Optional[Callable[[], None]] = None,
    ):
        self.fd = fd
        self.node_id = node_id
        self.stream_width = stream_width
        self.stream_height = stream_height
        self.fps = fps
        self.quality = max(10, min(100, quality))
        self.scale = scale
        self.is_mock = is_mock
        self.on_frame = on_frame
        self.on_error_or_eos = on_error_or_eos

        self.pipeline: Optional[Gst.Pipeline] = None
        self.bus: Optional[Gst.Bus] = None
        self.bus_watch_id: Optional[int] = None
        self._is_running = False

    def build_pipeline_description(self) -> str:
        """Constructs the GStreamer pipeline string."""
        scale_element = ""
        if self.scale != 1.0 and 0.1 <= self.scale <= 2.0:
            target_w = int(self.stream_width * self.scale)
            # Ensure width/height are even numbers for encoders
            target_w = target_w - (target_w % 2)
            target_h = int(self.stream_height * self.scale)
            target_h = target_h - (target_h % 2)
            scale_element = f"videoscale ! video/x-raw,width={target_w},height={target_h} ! "

        if self.is_mock:
            # Sandbox test pattern with animated ball and live clock overlay
            return (
                f"videotestsrc is-live=true pattern=ball ! "
                f"clockoverlay halignment=left valignment=bottom font-desc='Sans 26' shaded-background=true ! "
                f"videorate ! video/x-raw,framerate={self.fps}/1 ! "
                f"videoconvert ! "
                f"{scale_element}"
                f"jpegenc quality={self.quality} idct-method=ifast ! "
                f"appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
            )

        # Real PipeWire capture pipeline
        # Directly connect pipewiresrc to videoconvert without rigid caps constraints
        # so PipeWire can negotiate Mutter's native monitor format and refresh rate seamlessly.
        desc = (
            f"pipewiresrc fd={self.fd} path={self.node_id} do-timestamp=true ! "
            f"videoconvert ! "
            f"{scale_element}"
            f"jpegenc quality={self.quality} idct-method=ifast ! "
            f"appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )
        return desc

    def start(self):
        """Builds and starts the GStreamer pipeline."""
        if self._is_running:
            return

        desc = self.build_pipeline_description()
        print(f"[gstreamer] Launching pipeline: {desc}")
        try:
            self.pipeline = Gst.parse_launch(desc)
        except Exception as e:
            raise RuntimeError(f"Failed to build GStreamer pipeline: {e}")

        sink = self.pipeline.get_by_name("sink")
        if not sink:
            raise RuntimeError("Pipeline missing appsink 'sink'")

        sink.connect("new-sample", self._on_new_sample)

        # Bus monitoring
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message::error", self._on_bus_error)
        self.bus.connect("message::eos", self._on_bus_eos)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to set GStreamer pipeline to PLAYING state")

        self._is_running = True
        print("[gstreamer] Pipeline is PLAYING and capturing frames.")

    def _on_new_sample(self, sink) -> Gst.FlowReturn:
        """Invoked on each encoded JPEG frame."""
        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        if buf and self.on_frame:
            data = buf.extract_dup(0, buf.get_size())
            self.on_frame(data)

        return Gst.FlowReturn.OK

    def _on_bus_error(self, bus, message):
        err, debug = message.parse_error()
        print(f"[gstreamer] Pipeline Error: {err.message} (debug: {debug})", file=sys.stderr)
        self.stop()
        if self.on_error_or_eos:
            self.on_error_or_eos()

    def _on_bus_eos(self, bus, message):
        print("[gstreamer] Pipeline received EOS (End of Stream).")
        self.stop()
        if self.on_error_or_eos:
            self.on_error_or_eos()

    def stop(self):
        """Stops and tears down the GStreamer pipeline."""
        if not self._is_running:
            return
        self._is_running = False

        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

        if self.bus:
            self.bus.remove_signal_watch()
            self.bus = None

        print("[gstreamer] Pipeline stopped.")
