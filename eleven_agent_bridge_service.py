"""
Local bridge service for Beatbot's integrated ElevenAgents mode.

Exposes a tiny localhost HTTP API that the browser-based ElevenLabs JS SDK page
can call for safe tool execution. This keeps Firecrawl and Beatbot state on the
Python side while giving the voice agent a narrow, operator-safe tool surface.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock, Thread
from typing import Any, Dict, Optional, Tuple
import json
import logging

from controller.beatbot_state import BeatbotState
from services.current_info_service import CurrentInfoService


class ElevenAgentBridgeService:
    """Serve localhost-only tool endpoints for integrated ElevenAgents mode."""

    def __init__(self, current_info_service: CurrentInfoService, state: BeatbotState):
        self.current_info_service = current_info_service
        self.state = state
        self.logger = logging.getLogger(__name__)
        self._lock = RLock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[Thread] = None
        self._session_context: Dict[str, Any] = {}

    def start(self, session_context: Optional[Dict[str, Any]] = None) -> str:
        """Start the bridge server if needed and return its base URL."""
        with self._lock:
            self._session_context = dict(session_context or {})
            if self._server and self._thread and self._thread.is_alive():
                host, port = self._server.server_address[:2]
                return f"http://{host}:{port}"

            outer = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, format: str, *args) -> None:  # noqa: A003
                    outer.logger.debug("ElevenAgentBridge: " + format, *args)

                def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Allow-Headers", "Content-Type")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                    self.end_headers()
                    self.wfile.write(body)

                def do_OPTIONS(self) -> None:  # noqa: N802
                    self._send_json(200, {"ok": True})

                def do_GET(self) -> None:  # noqa: N802
                    if self.path == "/health":
                        self._send_json(200, {"ok": True})
                        return
                    if self.path == "/tool/beatbot-context":
                        self._send_json(200, outer._build_context_payload())
                        return
                    self._send_json(404, {"error": "Not found"})

                def do_POST(self) -> None:  # noqa: N802
                    content_length = int(self.headers.get("Content-Length", "0") or "0")
                    raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                    try:
                        payload = json.loads(raw_body.decode("utf-8") or "{}")
                    except Exception:
                        payload = {}

                    if self.path == "/tool/current-info":
                        query = str(payload.get("query", "") or "").strip()
                        intent = str(payload.get("intent", "general") or "general").strip()
                        if not query:
                            self._send_json(400, {"error": "query is required"})
                            return
                        try:
                            briefing = outer.current_info_service.get_briefing(
                                query=query,
                                intent=intent,
                                limit=5,
                            )
                            self._send_json(
                                200,
                                {
                                    "ok": True,
                                    "briefing": briefing,
                                    "backend": outer.current_info_service.get_backend_label(),
                                },
                            )
                        except Exception as e:
                            outer.logger.error("Current-info tool failed: %s", e)
                            self._send_json(500, {"error": str(e)})
                        return

                    self._send_json(404, {"error": "Not found"})

            self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self._thread = Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            host, port = self._server.server_address[:2]
            self.logger.info("ElevenAgentBridge started on %s:%s", host, port)
            return f"http://{host}:{port}"

    def stop(self) -> None:
        """Stop the bridge server if it is running."""
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server:
            try:
                server.shutdown()
                server.server_close()
            except Exception as e:
                self.logger.error("Error shutting down ElevenAgentBridge: %s", e)
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def update_session_context(self, session_context: Optional[Dict[str, Any]]) -> None:
        """Refresh the live session context available to client tools."""
        with self._lock:
            self._session_context = dict(session_context or {})

    def _build_context_payload(self) -> Dict[str, Any]:
        """Return a text-rich context payload for the agent."""
        with self._lock:
            session_context = dict(self._session_context)

        with self.state._lock:
            current_track = (
                f"{self.state.current_track.artist} - {self.state.current_track.title}"
                if self.state.current_track
                else ""
            )
            next_track = (
                f"{self.state.next_track.artist} - {self.state.next_track.title}"
                if self.state.next_track
                else ""
            )
            context_lines = [
                f"Event: {self.state.event_name or 'None'}",
                f"Venue: {self.state.venue_name}",
                f"Location: {self.state.location_name}",
                f"Audience: {self.state.audience_type}",
                f"Environment Notes: {self.state.environment_notes or 'None'}",
                f"Admin Notes: {self.state.admin_notes or 'None'}",
                f"Current Track: {current_track or 'None'}",
                f"Next Track: {next_track or 'None'}",
            ]

        if session_context:
            extra_lines = [
                f"{key}: {value}"
                for key, value in session_context.items()
                if value and key not in {"current_track", "next_track"}
            ]
            context_lines.extend(extra_lines)

        return {
            "ok": True,
            "context": "\n".join(context_lines),
            "session_context": session_context,
        }
