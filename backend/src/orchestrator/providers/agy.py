"""AGY (Gemini) CLI adapter — verified against agy 1.1.26.

Non-interactive: `agy --print <prompt> --output-format stream-json
--dangerously-skip-permissions --print-timeout <timeout>`
No cwd flag exists; the process cwd is set to the workspace instead.
"""

from __future__ import annotations

from .base import (
    ExecutionRequest,
    ProviderAdapter,
    ProviderCapabilities,
    extract_json_line,
)


class AgyAdapter(ProviderAdapter):
    name = "agy"
    executable = "agy"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming_json=True, supports_cwd_flag=False)

    def build_command(self, request: ExecutionRequest) -> list[str]:
        timeout_min = max(1, int(request.timeout_s // 60))
        return [
            self.executable,
            "--print", request.prompt,
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--print-timeout", f"{timeout_min}m",
        ]

    def normalize_output_line(self, line: str) -> str | None:
        event = extract_json_line(line)
        if event is None:
            return line or None
        # agy stream-json events (claude-code-compatible shape)
        etype = event.get("type")
        if etype == "assistant":
            parts = []
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool: {block.get('name', '?')}]")
            return "\n".join(p for p in parts if p) or None
        if etype == "result":
            return str(event.get("result") or f"[agy result: {event.get('subtype', '?')}]")
        return None

    def extract_summary(self, stdout_tail: list[str]) -> str:
        for line in reversed(stdout_tail):
            event = extract_json_line(line)
            if event and event.get("type") == "result" and event.get("result"):
                return str(event["result"])[:2000]
        return super().extract_summary(stdout_tail)
