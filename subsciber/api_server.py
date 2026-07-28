#!/usr/bin/env python3
"""
REST control API for stream_subscriber (OpenAPI / Swagger at /docs).

Runs uvicorn in a background thread. All player mutations are marshaled
onto the Qt GUI thread via StreamControlBridge.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import threading
from concurrent.futures import Future
from typing import Any, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from PyQt5.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT_BASE = 14400
DEFAULT_API_PORT = DEFAULT_API_PORT_BASE  # used when source index is 0


def api_port_for_source_index(idx: int) -> int:
    """REST API port = 14400 + source index in the topic/source list."""
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = 0
    if idx < 0:
        idx = 0
    return DEFAULT_API_PORT_BASE + idx


RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class TopicItem(BaseModel):
    index: int
    name: str
    url: str


class TopicCreate(BaseModel):
    name: str = Field(..., description="Display name shown in the topic combo")
    url: str = Field(..., description="Public or RTSP URL, e.g. http://10.1.106.210/ or rtsp://host:8554/stream")


class TopicSelect(BaseModel):
    index: Optional[int] = Field(None, description="Topic index (0-based)")
    name: Optional[str] = Field(None, description="Topic name (alternative to index)")


class StreamOpenRequest(BaseModel):
    index: Optional[int] = Field(None, description="Topic index to open; omit to use current selection")
    name: Optional[str] = Field(None, description="Topic name to open; alternative to index")


class RecordStartRequest(BaseModel):
    path: Optional[str] = Field(None, description="Optional output file path")


class RecordingItem(BaseModel):
    file: str
    size: int
    mtime: float


class StatusResponse(BaseModel):
    state: str
    recording: bool
    publishing: bool
    current_index: int
    current_name: str
    current_url: str
    fps: float
    bitrate_mbps: float
    topics: List[TopicItem]
    recording_file: Optional[str] = None
    last_saved_file: Optional[str] = None


class ActionResponse(BaseModel):
    ok: bool
    message: str
    file: Optional[str] = Field(None, description="Saved recording basename (when applicable)")
    status: Optional[StatusResponse] = None


# ---------------------------------------------------------------------------
# Qt bridge — run control actions on the GUI thread
# ---------------------------------------------------------------------------

class StreamControlBridge(QObject):
    """Queue control actions onto the Qt main thread and return results."""

    _invoke = pyqtSignal(str, dict, object)

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self._player = player
        self._invoke.connect(self._on_invoke)

    def call(self, action: str, timeout: float = 30.0, **params: Any) -> Any:
        future: Future = Future()
        self._invoke.emit(action, params, future)
        return future.result(timeout=timeout)

    def _on_invoke(self, action: str, params: dict, future: Future) -> None:
        try:
            handler = getattr(self, f"_do_{action}", None)
            if handler is None:
                raise ValueError(f"Unknown action: {action}")
            future.set_result(handler(**params))
        except Exception as exc:
            future.set_exception(exc)

    # -- handlers (GUI thread) ------------------------------------------------

    def _do_status(self) -> dict:
        return self._player.api_get_status()

    def _do_list_topics(self) -> list:
        return self._player.api_list_topics()

    def _do_add_topic(self, name: str, url: str) -> dict:
        return self._player.api_add_topic(name, url)

    def _do_remove_topic(self, index: int) -> dict:
        return self._player.api_remove_topic(index)

    def _do_select_topic(self, index: Optional[int] = None, name: Optional[str] = None) -> dict:
        return self._player.api_select_topic(index=index, name=name)

    def _do_open(self, index: Optional[int] = None, name: Optional[str] = None) -> dict:
        return self._player.api_open(index=index, name=name)

    def _do_close(self) -> dict:
        return self._player.api_close()

    def _do_record_start(self, path: Optional[str] = None) -> dict:
        return self._player.api_record_start(path=path)

    def _do_record_stop(self) -> dict:
        return self._player.api_record_stop()

    def _do_list_recordings(self) -> list:
        return self._player.api_list_recordings()

    def _do_recording_path(self, filename: str) -> str:
        return self._player.api_recording_path(filename)

    def _do_publish_start(self) -> dict:
        return self._player.api_publish_start()

    def _do_publish_stop(self) -> dict:
        return self._player.api_publish_stop()


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

def create_app(bridge: StreamControlBridge, recordings_dir: str = RECORDINGS_DIR) -> FastAPI:
    app = FastAPI(
        title="Stream Subscriber Control API",
        description=(
            "REST API for controlling the stream subscriber: "
            "open/close stream, record, download recordings, publish metrics to InfluxDB, "
            "and topic selection."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _call(action: str, timeout: float = 30.0, **params) -> Any:
        try:
            return bridge.call(action, timeout=timeout, **params)
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=f"Control timed out: {exc}") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _action_response(result: dict, default_message: str) -> ActionResponse:
        status = _call("status")
        return ActionResponse(
            ok=True,
            message=result.get("message", default_message),
            file=result.get("file"),
            status=status,
        )

    @app.get("/health", tags=["system"])
    def health():
        return {"ok": True}

    @app.get("/api/v1/status", response_model=StatusResponse, tags=["status"])
    def get_status():
        return _call("status")

    @app.get("/api/v1/topics", response_model=List[TopicItem], tags=["topics"])
    def list_topics():
        return _call("list_topics")

    @app.post("/api/v1/topics", response_model=ActionResponse, tags=["topics"])
    def add_topic(body: TopicCreate):
        result = _call("add_topic", name=body.name, url=body.url)
        return _action_response(result, "Topic added")

    @app.post("/api/v1/topics/select", response_model=ActionResponse, tags=["topics"])
    def select_topic(body: TopicSelect):
        result = _call("select_topic", index=body.index, name=body.name)
        return _action_response(result, "Topic selected")

    @app.delete("/api/v1/topics/{index}", response_model=ActionResponse, tags=["topics"])
    def remove_topic(index: int):
        result = _call("remove_topic", index=index)
        return _action_response(result, "Topic removed")

    @app.post("/api/v1/stream/open", response_model=ActionResponse, tags=["stream"])
    def stream_open(body: StreamOpenRequest = StreamOpenRequest()):
        result = _call("open", index=body.index, name=body.name)
        return _action_response(result, "Opening stream")

    @app.post("/api/v1/stream/close", response_model=ActionResponse, tags=["stream"])
    def stream_close():
        result = _call("close")
        return _action_response(result, "Stream closed")

    @app.post("/api/v1/record/start", response_model=ActionResponse, tags=["record"])
    def record_start(body: RecordStartRequest = RecordStartRequest()):
        result = _call("record_start", path=body.path)
        return _action_response(result, "Recording started")

    @app.post("/api/v1/record/stop", response_model=ActionResponse, tags=["record"])
    def record_stop():
        # stop_recording waits for mux finalization (can take several seconds)
        result = _call("record_stop", timeout=90.0)
        return _action_response(result, "Recording saved")

    @app.get("/api/v1/recordings", response_model=List[RecordingItem], tags=["record"])
    def list_recordings():
        return _call("list_recordings")

    @app.get("/api/v1/recordings/{filename}", tags=["record"])
    def download_recording(filename: str):
        """Download a saved recording by filename (basename only)."""
        path = _call("recording_path", filename=filename)
        media_type, _ = mimetypes.guess_type(path)
        # Python's mimetypes often mis-labels .ts (Qt linguist); MPEG-TS recordings need video/mp2t
        if path.lower().endswith((".ts", ".mts", ".m2ts")):
            media_type = "video/mp2t"
        elif path.lower().endswith(".mkv"):
            media_type = "video/x-matroska"
        return FileResponse(
            path,
            media_type=media_type or "application/octet-stream",
            filename=os.path.basename(path),
        )

    @app.post("/api/v1/publish/start", response_model=ActionResponse, tags=["publish"])
    def publish_start():
        result = _call("publish_start")
        return _action_response(result, "Publishing started")

    @app.post("/api/v1/publish/stop", response_model=ActionResponse, tags=["publish"])
    def publish_stop():
        result = _call("publish_stop")
        return _action_response(result, "Publishing stopped")

    return app


def iter_local_ipv4_addresses():
    """Yield IPv4 addresses from all up interfaces (excluding loopback duplicates later)."""
    import socket

    seen = set()
    try:
        import netifaces  # optional
        for iface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
            for entry in addrs:
                ip = entry.get("addr")
                if ip and ip not in seen:
                    seen.add(ip)
                    yield ip
        return
    except Exception:
        pass

    # Fallback without netifaces: UDP connect trick + hostname lookup
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and ip not in seen:
                seen.add(ip)
                yield ip
    except Exception:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in seen:
                seen.add(ip)
                yield ip
    except Exception:
        pass

    try:
        import subprocess
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                i = parts.index("inet")
                if i + 1 < len(parts):
                    ip = parts[i + 1].split("/")[0]
                    if ip and ip not in seen:
                        seen.add(ip)
                        yield ip
    except Exception:
        pass


def docs_urls_for_bind(host: str, port: int):
    """
    Return Swagger /docs URLs for printing.
    When bound to all interfaces (0.0.0.0 / ::), include every local IPv4.
    """
    urls = []
    bind_all = host in ("0.0.0.0", "::", "")
    if bind_all:
        urls.append(f"http://127.0.0.1:{port}/docs")
        for ip in iter_local_ipv4_addresses():
            if ip.startswith("127."):
                continue
            urls.append(f"http://{ip}:{port}/docs")
    else:
        display = "127.0.0.1" if host == "localhost" else host
        if ":" in display and not display.startswith("["):
            display = f"[{display}]"
        urls.append(f"http://{display}:{port}/docs")
    # de-dupe preserve order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _find_listener_pid(port: int) -> Optional[int]:
    """Best-effort PID of the process listening on TCP port."""
    try:
        import subprocess
        out = subprocess.check_output(
            ["ss", "-tlnp", f"sport = :{port}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        # users:(("python",pid=123,fd=19))
        m = re.search(r"pid=(\d+)", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.check_output(
            ["lsof", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
    except Exception:
        pass
    return None


def _port_available(host: str, port: int) -> bool:
    import socket

    bind_host = host if host not in ("0.0.0.0", "::", "") else "0.0.0.0"
    family = socket.AF_INET6 if ":" in bind_host and bind_host != "0.0.0.0" else socket.AF_INET
    if bind_host == "0.0.0.0":
        bind_host = ""
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_host, port))
        return True
    except OSError:
        return False


def start_api_server(
    bridge: StreamControlBridge,
    host: str = DEFAULT_API_HOST,
    port: int = DEFAULT_API_PORT,
    source_index: int = 0,
    topic_name: str = "",
):
    """Start uvicorn in a daemon thread. Returns (thread, server, docs_urls).

    Raises RuntimeError if the port cannot be bound.
    """
    import time
    import uvicorn

    if not _port_available(host, port):
        holder = _find_listener_pid(port)
        detail = f" (PID {holder})" if holder else ""
        raise RuntimeError(
            f"API port {port} already in use{detail}. "
            f"Select a different topic in the dropdown, or close the other subscriber."
        )

    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    app = create_app(bridge, recordings_dir=RECORDINGS_DIR)
    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=True)
    server = uvicorn.Server(config)
    # Avoid uvicorn installing its own signal handlers in a background thread
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    run_error: list = []

    def _run():
        try:
            server.run()
        except Exception as exc:
            run_error.append(exc)
            logger.error("API server exited with error: %s", exc)

    thread = threading.Thread(target=_run, name="subscriber-api", daemon=True)
    thread.start()

    # Wait until uvicorn reports started, or fail if bind/startup failed
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if getattr(server, "started", False):
            break
        if run_error:
            raise RuntimeError(f"API server failed to start on {host}:{port}: {run_error[0]}")
        if not thread.is_alive():
            holder = _find_listener_pid(port)
            detail = f" (held by PID {holder})" if holder else ""
            raise RuntimeError(
                f"API server failed to bind {host}:{port}{detail}. "
                "Another process is using this port."
            )
        time.sleep(0.05)
    else:
        if not getattr(server, "started", False):
            holder = _find_listener_pid(port)
            detail = f" (held by PID {holder})" if holder else ""
            raise RuntimeError(
                f"Timed out waiting for API bind on {host}:{port}{detail}."
            )

    docs = docs_urls_for_bind(host, port)
    print_api_links(host, port, source_index, topic_name=topic_name, docs=docs)
    return thread, server, docs


def print_api_links(
    host: str,
    port: int,
    source_index: int,
    topic_name: str = "",
    docs: Optional[List[str]] = None,
) -> List[str]:
    """Print Swagger /docs URLs for the current API bind (also on dropdown change)."""
    if docs is None:
        docs = docs_urls_for_bind(host, port)
    topic_label = f" ({topic_name})" if topic_name else ""
    print("\n========== Stream Subscriber Control API ==========", flush=True)
    print(
        f"Dropdown source index: {source_index}{topic_label}  →  port {port}  "
        f"(= {DEFAULT_API_PORT_BASE}+{source_index})",
        flush=True,
    )
    print(f"Listening on {host}:{port}", flush=True)
    print("Swagger docs:", flush=True)
    for url in docs:
        print(f"  {url}", flush=True)
    print("===================================================\n", flush=True)
    for url in docs:
        logger.info("Swagger UI: %s", url)
    return docs


def stop_api_server(server, thread, join_timeout: float = 3.0) -> None:
    """Ask uvicorn to exit and wait briefly for the thread to finish."""
    import time

    if server is None:
        return
    try:
        server.should_exit = True
        if getattr(server, "started", False):
            server.force_exit = True
    except Exception as exc:
        logger.warning("Error signaling API server shutdown: %s", exc)
    if thread is not None and thread.is_alive():
        thread.join(timeout=join_timeout)
        if thread.is_alive():
            logger.warning("API server thread did not exit within %.1fs", join_timeout)
    time.sleep(0.2)


class ApiServerManager:
    """Bind REST API to port 14400 + dropdown source index; rebind on selection change."""

    def __init__(self, player, host: str = DEFAULT_API_HOST, fixed_port: Optional[int] = None):
        from PyQt5.QtWidgets import QComboBox

        self._QComboBox = QComboBox
        self.player = player
        self.host = host
        self.fixed_port = fixed_port
        self.bridge = StreamControlBridge(player)
        self.thread = None
        self.server = None
        self.source_index = None
        self.port = None
        self.docs_urls = []

    def _combo(self):
        return self.player.widgetOpen.findChild(self._QComboBox, "comboBox_URL")

    def current_dropdown_index(self) -> int:
        combo = self._combo()
        if combo is None:
            return 0
        return max(0, combo.currentIndex())

    def current_dropdown_name(self) -> str:
        combo = self._combo()
        if combo is None:
            return ""
        return combo.currentText() or ""

    def start_for_index(self, source_index: Optional[int] = None, *, announce: bool = True):
        if source_index is None:
            source_index = self.current_dropdown_index()
        source_index = max(0, int(source_index))
        port = self.fixed_port if self.fixed_port is not None else api_port_for_source_index(source_index)
        topic_name = self.current_dropdown_name()

        if self.server is not None and getattr(self.server, "started", False) and self.port == port:
            # Same listen port (fixed --api-port, or same dropdown index)
            self.source_index = source_index
            if announce:
                self.docs_urls = print_api_links(
                    self.host, port, source_index, topic_name=topic_name, docs=self.docs_urls or None
                )
            return self.docs_urls

        self.stop()
        # start_api_server already prints links
        self.thread, self.server, self.docs_urls = start_api_server(
            self.bridge,
            host=self.host,
            port=port,
            source_index=source_index,
            topic_name=topic_name,
        )
        self.source_index = source_index
        self.port = port
        return self.docs_urls

    def restart_for_dropdown(self):
        idx = self.current_dropdown_index()
        name = self.current_dropdown_name()
        logger.info("Dropdown source changed → index=%s (%s), rebinding API port", idx, name)
        return self.start_for_index(idx, announce=True)

    def stop(self):
        if self.server is not None or (self.thread is not None and self.thread.is_alive()):
            print(f"Stopping API server on port {self.port}...", flush=True)
        stop_api_server(self.server, self.thread)
        self.server = None
        self.thread = None
        self.port = None
        self.source_index = None
        self.docs_urls = []
