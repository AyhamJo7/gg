"""Claude Code CLI adapter — verified against claude 2.1.261.

Non-interactive: `claude -p <prompt> --output-format stream-json --verbose
--permission-mode bypassPermissions`
stream-json emits NDJSON events; we extract readable text from assistant
messages and the final result.
"""

from __future__ import annotations

from .base import (
    ExecutionRequest,
    ProviderAdapter,
    ProviderCapabilities,
    extract_json_line,
)


class ClaudeAdapter(ProviderAdapter):
    name = "claude"
    executable = "claude"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming_json=True, supports_cwd_flag=False)

    def build_command(self, request: ExecutionRequest) -> list[str]:
        return [
            self.executable,
            "-p", request.prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "bypassPermissions",
        ]

    def normalize_output_line(self, line: str) -> str | None:
        event = extract_json_line(line)
        if event is None:
            return line or None
        etype = event.get("type")
        if etype == "assistant":
            message = event.get("message", {})
            parts = []
            for block in message.get("content", []):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool: {block.get('name', '?')}]")
            return "\n".join(p for p in parts if p) or None
        if etype == "result":
            return f"[claude result: {event.get('subtype', '?')}]"
        if etype == "system":
            return None
        return None

    def extract_assistant_text(self, stdout_tail: list[str]) -> str:
        """Reconstruct assistant response from NDJSON stream-json events."""
        parts: list[str] = []
        for line in stdout_tail:
            event = extract_json_line(line)
            if not event:
                continue
            etype = event.get("type")
            if etype == "assistant":
                message = event.get("message", {})
                for block in message.get("content", []):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
            elif etype == "result":
                res = event.get("result")
                if isinstance(res, str):
                    parts.append(res)
                elif isinstance(res, dict):
                    response = res.get("response") or res.get("text") or str(res)
                    parts.append(response)
        return "\n".join(parts)

    def extract_summary(self, stdout_tail: list[str]) -> str:
        # Prefer the final result event's text over the last arbitrary line.
        for line in reversed(stdout_tail):
            event = extract_json_line(line)
            if event and event.get("type") == "result" and event.get("result"):
                return str(event["result"])[:2000]
        return super().extract_summary(stdout_tail)

    def is_success_marker(self, text: str) -> bool:
        normalized = text.replace(" ", "")
        # A genuine success marker must NOT be accompanied by an explicit error flag.
        if '"is_error":true' in normalized or '"error":' in normalized:
            return False
        return '"subtype":"success"' in normalized or '"type":"result"' in normalized
