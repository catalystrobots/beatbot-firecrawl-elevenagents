"""
Live conversation service for Beatbot.

Opens an external ElevenLabs widget and monitors browser audio sessions on
Windows so Beatbot can detect speaking activity, drive simple animations, and
auto-return to normal hosting after the conversation goes quiet.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.eleven_agents_service import ElevenAgentsLaunchDescriptor, ElevenAgentsService

try:
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

    PYCAW_AVAILABLE = True
except Exception:
    AudioUtilities = None  # type: ignore
    IAudioMeterInformation = None  # type: ignore
    PYCAW_AVAILABLE = False

try:
    import pythoncom

    def _co_initialize() -> None:
        pythoncom.CoInitialize()

    def _co_uninitialize() -> None:
        pythoncom.CoUninitialize()

    COM_MONITORING_AVAILABLE = True
except Exception:
    pythoncom = None  # type: ignore
    try:
        from comtypes import CoInitialize, CoUninitialize

        def _co_initialize() -> None:
            CoInitialize()

        def _co_uninitialize() -> None:
            CoUninitialize()

        COM_MONITORING_AVAILABLE = True
    except Exception:
        COM_MONITORING_AVAILABLE = False

        def _co_initialize() -> None:
            raise RuntimeError("COM initialization is unavailable")

        def _co_uninitialize() -> None:
            return

LIVE_AUDIO_MONITORING_AVAILABLE = PYCAW_AVAILABLE and COM_MONITORING_AVAILABLE


@dataclass
class LiveConversationSnapshot:
    """Runtime status for operator UI updates."""

    active: bool = False
    speaking: bool = False
    status: str = "inactive"
    audio_level: float = 0.0
    active_browser: str = ""
    end_reason: str = ""
    backend: str = ""


class LiveConversationService:
    """Manage widget launch and browser-audio-based conversation detection."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.logger = logging.getLogger(__name__)
        self.config = config or {}
        self.live_config = self.config.get("live_conversation", {})
        self.eleven_agents_service = ElevenAgentsService(config)

        self.enabled = bool(self.live_config.get("enabled", True))
        self.agent_id = str(self.live_config.get("agent_id", "") or "").strip()
        self.widget_url = self._resolve_fallback_widget_url()
        self.audio_threshold = float(self.live_config.get("audio_threshold", 0.012))
        self.speaking_silence_seconds = float(
            self.live_config.get("speaking_silence_seconds", 1.0)
        )
        self.auto_end_silence_seconds = float(
            self.live_config.get("auto_end_silence_seconds", 15.0)
        )
        self.poll_interval_seconds = float(
            self.live_config.get("poll_interval_seconds", 0.05)
        )
        configured_processes = self.live_config.get(
            "browser_processes",
            ["chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe"],
        )
        self.browser_processes = [
            str(name).strip().lower() for name in configured_processes if str(name).strip()
        ]

        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._speaking = False
        self._last_audio_time = 0.0
        self._last_activity_time = 0.0
        self._audio_level = 0.0
        self._active_browser = ""
        self._last_end_reason = ""
        self._backend_label = ""

        self._status_callbacks: List[Callable[[LiveConversationSnapshot], None]] = []
        self._speaking_callbacks: List[Callable[[bool, LiveConversationSnapshot], None]] = []
        self._session_end_callbacks: List[Callable[[str, LiveConversationSnapshot], None]] = []

    def _resolve_fallback_widget_url(self) -> str:
        """Build a usable fallback widget URL from config."""
        widget_url = str(self.live_config.get("widget_url", "") or "").strip()
        if widget_url:
            return widget_url
        if self.agent_id:
            return f"https://elevenlabs.io/app/talk-to?agent_id={self.agent_id}"
        return ""

    @property
    def monitoring_supported(self) -> bool:
        """Whether this runtime can monitor widget audio reliably."""
        return sys.platform.startswith("win") and LIVE_AUDIO_MONITORING_AVAILABLE

    def add_status_callback(self, callback: Callable[[LiveConversationSnapshot], None]) -> None:
        self._status_callbacks.append(callback)

    def add_speaking_callback(
        self,
        callback: Callable[[bool, LiveConversationSnapshot], None],
    ) -> None:
        self._speaking_callbacks.append(callback)

    def add_session_end_callback(
        self,
        callback: Callable[[str, LiveConversationSnapshot], None],
    ) -> None:
        self._session_end_callbacks.append(callback)

    def get_snapshot(self) -> LiveConversationSnapshot:
        """Return a copy of the current runtime state."""
        with self._lock:
            status = "inactive"
            if self._running:
                status = "speaking" if self._speaking else "listening"
            return LiveConversationSnapshot(
                active=self._running,
                speaking=self._speaking,
                status=status,
                audio_level=self._audio_level,
                active_browser=self._active_browser,
                end_reason=self._last_end_reason,
                backend=self._backend_label,
            )

    def start(
        self,
        *,
        mode: str = "classic",
        session_context: Optional[Dict[str, Any]] = None,
        bridge_base_url: str = "",
    ) -> Tuple[bool, str]:
        """Open the selected live-chat surface and start browser-audio monitoring."""
        try:
            launch = self._resolve_launch(
                mode=mode,
                session_context=session_context,
                bridge_base_url=bridge_base_url,
            )
        except Exception as e:
            return False, f"Could not prepare the live conversation session: {e}"

        with self._lock:
            if self._running:
                return True, "Live conversation mode is already active."

            if not self.enabled:
                return False, "Live conversation mode is disabled in configuration."
            if not self.monitoring_supported:
                return (
                    False,
                    "Live conversation audio monitoring requires Windows plus pycaw and COM support.",
                )

            self._running = True
            self._speaking = False
            self._audio_level = 0.0
            self._active_browser = ""
            now = time.monotonic()
            self._last_audio_time = now
            self._last_activity_time = now
            self._last_end_reason = ""
            self._backend_label = launch.backend_label
            self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._thread.start()

        try:
            webbrowser.open(launch.launch_url)
        except Exception as e:
            self.stop(reason=f"widget open failed: {e}")
            return False, f"Could not open the live conversation page: {e}"

        self._emit_status()
        self.logger.info("Live conversation mode started via %s", launch.backend_label)
        return True, f"Live conversation mode started via {launch.backend_label}."

    def _resolve_launch(
        self,
        *,
        mode: str,
        session_context: Optional[Dict[str, Any]],
        bridge_base_url: str,
    ):
        """Resolve the launch target for the requested live conversation mode."""
        normalized_mode = str(mode or "classic").strip().lower()
        if normalized_mode == "integrated":
            return self.eleven_agents_service.resolve_integrated_launch(
                bridge_base_url=bridge_base_url,
                session_context=session_context,
            )

        if not self.widget_url:
            raise RuntimeError("Set live_conversation.widget_url or live_conversation.agent_id first.")
        return ElevenAgentsLaunchDescriptor(
            launch_url=self.widget_url,
            backend_label="ElevenLabs Talk-To Page",
            uses_signed_url=False,
        )

    def stop(self, reason: str = "manual stop") -> None:
        """Stop monitoring and end the current session."""
        thread = None
        with self._lock:
            if not self._running and not self._last_end_reason:
                return
            self._running = False
            thread = self._thread

        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

        self._finalize(reason)

    def _monitor_loop(self) -> None:
        """Watch browser audio levels and infer speaking/idle/end transitions."""
        if not LIVE_AUDIO_MONITORING_AVAILABLE:
            self._finalize("monitor unavailable")
            return

        _co_initialize()
        end_reason = "manual stop"
        try:
            while True:
                with self._lock:
                    if not self._running:
                        break

                audio_level, active_browser = self._read_browser_audio_level()
                now = time.monotonic()

                with self._lock:
                    self._audio_level = audio_level
                    self._active_browser = active_browser

                    if audio_level > self.audio_threshold:
                        self._last_audio_time = now
                        self._last_activity_time = now
                        if not self._speaking:
                            self._speaking = True
                            snapshot = self.get_snapshot()
                            for callback in self._speaking_callbacks:
                                try:
                                    callback(True, snapshot)
                                except Exception as e:
                                    self.logger.error(f"Live speaking callback failed: {e}")
                            self._emit_status(snapshot)
                    elif self._speaking and (now - self._last_audio_time) >= self.speaking_silence_seconds:
                        self._speaking = False
                        snapshot = self.get_snapshot()
                        for callback in self._speaking_callbacks:
                            try:
                                callback(False, snapshot)
                            except Exception as e:
                                self.logger.error(f"Live speaking callback failed: {e}")
                        self._emit_status(snapshot)

                    if (now - self._last_activity_time) >= self.auto_end_silence_seconds:
                        self._running = False
                        end_reason = "silence timeout"
                        break

                time.sleep(self.poll_interval_seconds)
        except Exception as e:
            self.logger.error(f"Live conversation monitoring failed: {e}")
            end_reason = f"monitor error: {e}"
        finally:
            _co_uninitialize()
            self._finalize(end_reason)

    def _read_browser_audio_level(self) -> Tuple[float, str]:
        """Return the loudest matching browser audio session level."""
        if AudioUtilities is None or IAudioMeterInformation is None:
            return 0.0, ""

        max_level = 0.0
        active_browser = ""

        try:
            sessions = AudioUtilities.GetAllSessions()
        except Exception as e:
            self.logger.debug(f"Could not enumerate audio sessions: {e}")
            return 0.0, ""

        for session in sessions:
            try:
                if not session.Process:
                    continue
                process_name = session.Process.name().lower()
                if process_name not in self.browser_processes:
                    continue

                meter = session._ctl.QueryInterface(IAudioMeterInformation)
                level = float(meter.GetPeakValue())
                if level > max_level:
                    max_level = level
                    active_browser = process_name
            except Exception:
                continue

        return max_level, active_browser

    def _finalize(self, reason: str) -> None:
        """Emit a single terminal event for the ended session."""
        callbacks: List[Callable[[str, LiveConversationSnapshot], None]] = []
        snapshot = None

        with self._lock:
            already_finished = not self._running and self._last_end_reason == reason
            if already_finished:
                return

            was_running = self._running or self._speaking or not self._last_end_reason
            self._running = False
            self._speaking = False
            self._audio_level = 0.0
            self._active_browser = ""
            self._thread = None
            self._last_end_reason = reason
            snapshot = self.get_snapshot()
            callbacks = list(self._session_end_callbacks) if was_running else []

        self._emit_status(snapshot)
        if snapshot:
            for callback in callbacks:
                try:
                    callback(reason, snapshot)
                except Exception as e:
                    self.logger.error(f"Live session end callback failed: {e}")

        self.logger.info("Live conversation mode ended: %s", reason)

    def _emit_status(self, snapshot: Optional[LiveConversationSnapshot] = None) -> None:
        """Notify UI listeners of status changes."""
        snapshot = snapshot or self.get_snapshot()
        for callback in self._status_callbacks:
            try:
                callback(snapshot)
            except Exception as e:
                self.logger.error(f"Live status callback failed: {e}")
