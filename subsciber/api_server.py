#!/usr/bin/env python3
"""
REST control API for stream_subscriber (OpenAPI / Swagger at /docs).

Runs uvicorn in a background thread. All player mutations are marshaled
onto the Qt GUI thread via StreamControlBridge.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from PyQt5.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 8081


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


class ActionResponse(BaseModel):
    ok: bool
    message: str
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

    def _do_publish_start(self) -> dict:
        return self._player.api_publish_start()

    def _do_publish_stop(self) -> dict:
        return self._player.api_publish_stop()


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

def create_app(bridge: StreamControlBridge) -> FastAPI:
    app = FastAPI(
        title="Stream Subscriber Control API",
        description=(
            "REST API for controlling the stream subscriber: "
            "open/close stream, record, publish metrics to InfluxDB, and topic selection."
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

    def _call(action: str, **params) -> Any:
        try:
            return bridge.call(action, **params)
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=f"Control timed out: {exc}") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        status = _call("status")
        return ActionResponse(ok=True, message=result.get("message", "Topic added"), status=status)

    @app.post("/api/v1/topics/select", response_model=ActionResponse, tags=["topics"])
    def select_topic(body: TopicSelect):
        result = _call("select_topic", index=body.index, name=body.name)
        status = _call("status")
        return ActionResponse(ok=True, message=result.get("message", "Topic selected"), status=status)

    @app.delete("/api/v1/topics/{index}", response_model=ActionResponse, tags=["topics"])
    def remove_topic(index: int):
        result = _call("remove_topic", index=index)
        status = _call("status")
        return ActionResponse(ok=True, message=result.get("message", "Topic removed"), status=status)

    @app.post("/api/v1/stream/open", response_model=ActionResponse, tags=["stream"])
    def stream_open(body: StreamOpenRequest = StreamOpenRequest()):
        result = _call("open", index=body.index, name=body.name)
        status = _call("status")
        return ActionResponse(ok=True, message=result.get("message", "Opening stream"), status=status)

    @app.post("/api/v1/stream/close", response_model=ActionResponse, tags=["stream"])
    def stream_close():
        result = _call("close")
        status = _call("status")
        return ActionResponse(ok=True, message=result.get("message", "Stream closed"), status=status)

    @app.post("/api/v1/record/start", response_model=ActionResponse, tags=["record"])
    def record_start(body: RecordStartRequest = RecordStartRequest()):
        result = _call("record_start", path=body.path)
        status = _call("status")
        return ActionResponse(ok=True, message=result.get("message", "Recording started"), status=status)

    @app.post("/api/v1/record/stop", response_model=ActionResponse, tags=["record"])
    def record_stop():
        result = _call("record_stop")
        status = _call("status")
        return ActionResponse(ok=True, message=result.get("message", "Recording stopped"), status=status)

    @app.post("/api/v1/publish/start", response_model=ActionResponse, tags=["publish"])
    def publish_start():
        result = _call("publish_start")
        status = _call("status")
        return ActionResponse(ok=True, message=result.get("message", "Publishing started"), status=status)

    @app.post("/api/v1/publish/stop", response_model=ActionResponse, tags=["publish"])
    def publish_stop():
        result = _call("publish_stop")
        status = _call("status")
        return ActionResponse(ok=True, message=result.get("message", "Publishing stopped"), status=status)

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

    # Fallback without netifaces: UDP connect trick + hostname lookup + /proc
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


def start_api_server(bridge: StreamControlBridge, host: str = DEFAULT_API_HOST, port: int = DEFAULT_API_PORT):
    """Start uvicorn in a daemon thread. Returns the thread."""
    import uvicorn

    app = create_app(bridge)
    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=True)
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, name="subscriber-api", daemon=True)
    thread.start()

    docs = docs_urls_for_bind(host, port)
    print("\n========== Stream Subscriber Control API ==========", flush=True)
    print(f"Listening on {host}:{port}", flush=True)
    print("Swagger docs:", flush=True)
    for url in docs:
        print(f"  {url}", flush=True)
    print("===================================================\n", flush=True)
    for url in docs:
        logger.info("Swagger UI: %s", url)
    return thread, server, docs
