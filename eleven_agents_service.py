"""
ElevenAgents helper service for Beatbot.

Builds a first-class embedded ElevenLabs widget launch page and can request
signed URLs for private agents. This keeps Beatbot on the current browser/audio
monitoring path while making the agent integration explicit and safer to evolve.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import html
import json
import logging
import os

import requests


@dataclass(frozen=True, slots=True)
class ElevenAgentsLaunchDescriptor:
    """Resolved launch target for a live conversation session."""

    launch_url: str
    backend_label: str
    uses_signed_url: bool = False


class ElevenAgentsService:
    """Prepare embedded widget and integrated-agent launches for ElevenAgents."""

    SIGNED_URL_ENDPOINT = "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.logger = logging.getLogger(__name__)
        self.config = config or {}
        self.live_config = self.config.get("live_conversation", {})
        self.tts_config = self.config.get("tts", {})
        self.paths_config = self.config.get("paths", {})

        self.agent_id = str(self.live_config.get("agent_id", "") or "").strip()
        self.widget_url = str(self.live_config.get("widget_url", "") or "").strip()
        self.use_embedded_widget = bool(self.live_config.get("use_embedded_widget", True))
        self.private_agent = bool(self.live_config.get("private_agent", False))
        self.widget_variant = str(self.live_config.get("widget_variant", "expanded") or "expanded").strip()
        self.widget_dismissible = bool(self.live_config.get("widget_dismissible", True))
        self.server_location = str(self.live_config.get("server_location", "us") or "us").strip()
        self.allow_fallback_widget_url = bool(self.live_config.get("allow_fallback_widget_url", True))
        self.api_key = str(
            self.live_config.get("api_key")
            or self.tts_config.get("api_key")
            or os.environ.get("ELEVENLABS_API_KEY")
            or ""
        ).strip()

        logs_dir = Path(self.paths_config.get("logs_dir", "./logs/")).resolve()
        self.output_dir = logs_dir / "eleven_agents"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def resolve_launch(self, session_context: Optional[Dict[str, Any]] = None) -> ElevenAgentsLaunchDescriptor:
        """Resolve the best available launch target for the configured agent."""
        if self.use_embedded_widget and self.agent_id:
            try:
                signed_url = self._get_signed_url() if self.private_agent else ""
                widget_page = self._write_widget_page(
                    agent_id=self.agent_id,
                    signed_url=signed_url,
                    session_context=session_context or {},
                )
                return ElevenAgentsLaunchDescriptor(
                    launch_url=widget_page.as_uri(),
                    backend_label="ElevenAgents Embedded Widget" + (" (signed)" if signed_url else ""),
                    uses_signed_url=bool(signed_url),
                )
            except Exception as e:
                self.logger.warning("Embedded ElevenAgents launch failed: %s", e)
                if not self.allow_fallback_widget_url:
                    raise

        fallback_url = self._resolve_fallback_widget_url()
        if fallback_url:
            return ElevenAgentsLaunchDescriptor(
                launch_url=fallback_url,
                backend_label="ElevenLabs Talk-To Page",
                uses_signed_url=False,
            )

        raise RuntimeError("Set live_conversation.agent_id or live_conversation.widget_url first.")

    def resolve_integrated_launch(
        self,
        *,
        bridge_base_url: str,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> ElevenAgentsLaunchDescriptor:
        """Resolve the integrated-agent launch target using the JS SDK."""
        if not self.agent_id:
            raise RuntimeError("Set live_conversation.agent_id for integrated agent mode.")
        if not bridge_base_url:
            raise RuntimeError("Integrated agent mode requires a local bridge URL.")

        signed_url = self._get_signed_url() if self.private_agent else ""
        page_path = self._write_integrated_agent_page(
            agent_id=self.agent_id,
            signed_url=signed_url,
            bridge_base_url=bridge_base_url,
            session_context=session_context or {},
        )
        return ElevenAgentsLaunchDescriptor(
            launch_url=page_path.as_uri(),
            backend_label="Integrated ElevenAgent" + (" (signed)" if signed_url else ""),
            uses_signed_url=bool(signed_url),
        )

    def _resolve_fallback_widget_url(self) -> str:
        """Return the legacy widget URL if available."""
        if self.widget_url:
            return self.widget_url
        if self.agent_id:
            return f"https://elevenlabs.io/app/talk-to?agent_id={self.agent_id}"
        return ""

    def _get_signed_url(self) -> str:
        """Request a temporary signed URL for a private agent."""
        if not self.private_agent:
            return ""
        if not self.api_key:
            raise RuntimeError("Set live_conversation.api_key or ELEVENLABS_API_KEY for private agents.")

        response = requests.get(
            self.SIGNED_URL_ENDPOINT,
            headers={"xi-api-key": self.api_key},
            params={"agent_id": self.agent_id},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        signed_url = str(payload.get("signed_url", "") or "").strip()
        if not signed_url:
            raise RuntimeError("ElevenLabs did not return a signed_url.")
        return signed_url

    def _write_widget_page(
        self,
        *,
        agent_id: str,
        signed_url: str,
        session_context: Dict[str, Any],
    ) -> Path:
        """Generate a local HTML wrapper around the ElevenAgents widget."""
        widget_attrs = [
            f'variant="{html.escape(self.widget_variant, quote=True)}"',
            f'server-location="{html.escape(self.server_location, quote=True)}"',
            f'dismissible="{str(self.widget_dismissible).lower()}"',
        ]
        if signed_url:
            widget_attrs.append(f'signed-url="{html.escape(signed_url, quote=True)}"')
        else:
            widget_attrs.append(f'agent-id="{html.escape(agent_id, quote=True)}"')

        context_json = json.dumps(session_context or {}, ensure_ascii=True, indent=2)
        context_pretty = html.escape(context_json)
        session_title = html.escape(str(session_context.get("event_name") or "Beatbot Live Conversation"))

        html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{session_title}</title>
  <style>
    body {{
      margin: 0;
      font-family: Segoe UI, Arial, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      min-height: 100vh;
      display: grid;
      place-items: center;
    }}
    .shell {{
      width: min(960px, 96vw);
      padding: 24px;
      box-sizing: border-box;
    }}
    .card {{
      background: rgba(15, 23, 42, 0.88);
      border: 1px solid rgba(148, 163, 184, 0.28);
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.28);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 20px;
      font-weight: 600;
    }}
    p {{
      margin: 0 0 14px;
      color: #cbd5e1;
    }}
    pre {{
      margin: 16px 0 0;
      padding: 12px;
      border-radius: 10px;
      background: #020617;
      color: #93c5fd;
      overflow: auto;
      font-size: 12px;
      line-height: 1.45;
    }}
    .widget-wrap {{
      margin-top: 18px;
      display: flex;
      justify-content: center;
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="card">
      <h1>Beatbot ElevenAgents Session</h1>
      <p>This embedded widget keeps Beatbot on the current browser-audio path while using ElevenAgents directly.</p>
      <div class="widget-wrap">
        <elevenlabs-convai {' '.join(widget_attrs)}></elevenlabs-convai>
      </div>
      <pre>{context_pretty}</pre>
    </div>
  </div>
  <script>
    window.beatbotSessionContext = {json.dumps(session_context or {}, ensure_ascii=True)};
  </script>
  <script src="https://unpkg.com/@elevenlabs/convai-widget-embed" async type="text/javascript"></script>
</body>
</html>
"""
        page_path = self.output_dir / "live_conversation_widget.html"
        page_path.write_text(html_text, encoding="utf-8")
        return page_path

    def _write_integrated_agent_page(
        self,
        *,
        agent_id: str,
        signed_url: str,
        bridge_base_url: str,
        session_context: Dict[str, Any],
    ) -> Path:
        """Generate a local HTML page that starts an ElevenAgents JS SDK session."""
        session_title = html.escape(str(session_context.get("event_name") or "Beatbot Integrated Agent"))
        page_config = {
            "agentId": agent_id,
            "signedUrl": signed_url,
            "bridgeBaseUrl": bridge_base_url,
            "sessionContext": session_context or {},
        }
        config_json = json.dumps(page_config, ensure_ascii=True)
        context_pretty = html.escape(json.dumps(session_context or {}, ensure_ascii=True, indent=2))

        html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{session_title}</title>
  <style>
    body {{
      margin: 0;
      font-family: Segoe UI, Arial, sans-serif;
      background: linear-gradient(180deg, #020617, #0f172a);
      color: #e2e8f0;
    }}
    .shell {{
      max-width: 1040px;
      margin: 0 auto;
      padding: 24px;
    }}
    .card {{
      background: rgba(15, 23, 42, 0.92);
      border: 1px solid rgba(148, 163, 184, 0.22);
      border-radius: 18px;
      padding: 18px;
      margin-bottom: 16px;
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.28);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 22px;
    }}
    p {{
      color: #cbd5e1;
      margin: 0 0 14px;
    }}
    .controls {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      margin: 12px 0 0;
    }}
    button {{
      border: 0;
      border-radius: 10px;
      padding: 10px 14px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      background: #2563eb;
      color: #fff;
    }}
    button.secondary {{
      background: #334155;
    }}
    button:disabled {{
      opacity: 0.55;
      cursor: not-allowed;
    }}
    .status {{
      display: inline-block;
      min-width: 180px;
      color: #93c5fd;
      font-weight: 600;
    }}
    pre {{
      margin: 0;
      padding: 12px;
      border-radius: 10px;
      background: #020617;
      overflow: auto;
      font-size: 12px;
      line-height: 1.45;
      color: #cbd5e1;
      white-space: pre-wrap;
    }}
    .hint {{
      color: #94a3b8;
      font-size: 13px;
      margin-top: 10px;
    }}
    .log {{
      min-height: 220px;
    }}
    .success {{
      color: #86efac;
    }}
    .warn {{
      color: #fbbf24;
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="card">
      <h1>Beatbot Integrated ElevenAgent</h1>
      <p>This mode keeps Beatbot's live-safe room handling but gives the voice agent access to approved Beatbot tools.</p>
      <div class="controls">
        <button id="start-btn">Start Integrated Agent</button>
        <button id="end-btn" class="secondary" disabled>End Session</button>
        <span id="status" class="status">Idle</span>
      </div>
      <div class="hint">
        Configure these client tools on your ElevenLabs agent to test the full bridge:
        <strong>lookupCurrentInfo(query, intent)</strong> and <strong>getBeatbotContext()</strong>.
      </div>
    </div>
    <div class="card">
      <h1>Beatbot Context</h1>
      <pre>{context_pretty}</pre>
    </div>
    <div class="card">
      <h1>Bridge Log</h1>
      <pre id="log" class="log"></pre>
    </div>
  </div>
  <script type="module">
    import {{ Conversation }} from "https://esm.sh/@elevenlabs/client";

    const config = {config_json};
    const statusEl = document.getElementById("status");
    const logEl = document.getElementById("log");
    const startBtn = document.getElementById("start-btn");
    const endBtn = document.getElementById("end-btn");
    let conversation = null;

    function log(message, kind = "") {{
      const line = `[${{new Date().toLocaleTimeString()}}] ${{message}}`;
      logEl.textContent = `${{line}}\n${{logEl.textContent}}`;
      if (kind === "error") {{
        console.error(message);
      }} else {{
        console.log(message);
      }}
    }}

    function setStatus(text) {{
      statusEl.textContent = text;
    }}

    async function callBridge(path, payload = null) {{
      const options = {{
        method: payload ? "POST" : "GET",
        headers: {{ "Content-Type": "application/json" }},
      }};
      if (payload) {{
        options.body = JSON.stringify(payload);
      }}
      const response = await fetch(`${{config.bridgeBaseUrl}}${{path}}`, options);
      const data = await response.json();
      if (!response.ok) {{
        throw new Error(data.error || `Bridge request failed: ${{response.status}}`);
      }}
      return data;
    }}

    async function startIntegratedSession() {{
      startBtn.disabled = true;
      setStatus("Requesting microphone...");
      try {{
        await navigator.mediaDevices.getUserMedia({{ audio: true }});
        log("Microphone granted.");

        const clientTools = {{
          lookupCurrentInfo: async (params = {{}}) => {{
            const query = String(params.query || "").trim();
            const intent = String(params.intent || "general").trim();
            log(`lookupCurrentInfo called for "${{query}}" (${{intent}})`);
            const result = await callBridge("/tool/current-info", {{ query, intent }});
            log(`lookupCurrentInfo resolved via ${{result.backend}}`);
            return result.briefing || "No approved-source result available.";
          }},
          getBeatbotContext: async () => {{
            log("getBeatbotContext called");
            const result = await callBridge("/tool/beatbot-context");
            return result.context || "No Beatbot context available.";
          }},
        }};

        const sessionOptions = {{
          clientTools,
          onConnect: () => {{
            setStatus("Connected");
            endBtn.disabled = false;
            log("Integrated ElevenAgent connected.", "success");
          }},
          onDisconnect: () => {{
            setStatus("Disconnected");
            endBtn.disabled = true;
            startBtn.disabled = false;
            log("Integrated ElevenAgent disconnected.", "warn");
          }},
          onStatusChange: (status) => {{
            setStatus(String(status || "Running"));
          }},
          onError: (error) => {{
            const message = error?.message || String(error);
            setStatus("Error");
            log(`Integrated agent error: ${{message}}`, "error");
            startBtn.disabled = false;
          }},
        }};

        if (config.signedUrl) {{
          sessionOptions.signedUrl = config.signedUrl;
          sessionOptions.connectionType = "websocket";
        }} else {{
          sessionOptions.agentId = config.agentId;
          sessionOptions.connectionType = "webrtc";
        }}

        setStatus("Connecting...");
        log("Starting integrated ElevenAgent session...");
        conversation = await Conversation.startSession(sessionOptions);
      }} catch (error) {{
        const message = error?.message || String(error);
        setStatus("Failed");
        log(`Could not start integrated agent: ${{message}}`, "error");
        startBtn.disabled = false;
      }}
    }}

    async function endIntegratedSession() {{
      if (!conversation) {{
        return;
      }}
      try {{
        if (typeof conversation.endSession === "function") {{
          await conversation.endSession();
        }}
      }} catch (error) {{
        log(`Error ending session: ${{error?.message || String(error)}}`, "error");
      }} finally {{
        conversation = null;
        endBtn.disabled = true;
        startBtn.disabled = false;
        setStatus("Ended");
      }}
    }}

    startBtn.addEventListener("click", startIntegratedSession);
    endBtn.addEventListener("click", endIntegratedSession);
    log("Integrated agent page ready.");
  </script>
</body>
</html>
"""
        page_path = self.output_dir / "integrated_agent_mode.html"
        page_path.write_text(html_text, encoding="utf-8")
        return page_path
