"""OpenCode CLI adapter — verified against opencode 1.17.13.

Non-interactive: `opencode run --format json --auto --dir <workdir> <message>`
`--format json` emits JSON events with `type`/`part` structure.
"""

from __future__ import annotations

from .base import (
    ExecutionRequest,
    ProviderAdapter,
    ProviderCapabilities,
    extract_json_line,
)


class OpencodeAdapter(ProviderAdapter):
    name = "opencode"
    executable = "opencode"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming_json=True, supports_cwd_flag=True)

    def build_command(self, request: ExecutionRequest) -> list[str]:
        return [
            self.executable, "run",
            "--format", "json",
            "--auto",
            "--dir", str(request.workdir),
            request.prompt,
        ]

    def normalize_output_line(self, line: str) -> str | None:
        event = extract_json_line(line)
        if event is None:
            return line or None
        part = event.get("part", {})
        ptype = part.get("type")
        if ptype == "text" and part.get("text"):
            return str(part["text"])
        if ptype == "tool":
            return f"[tool: {part.get('tool', '?')}]"
        if event.get("type") == "error":
            return f"[opencode error] {event}"
        return None

    def extract_summary(self, stdout_tail: list[str]) -> str:
        texts: list[str] = []
        for line in stdout_tail:
            event = extract_json_line(line)
            if event and event.get("part", {}).get("type") == "text" and event["part"].get("text"):
                texts.append(str(event["part"]["text"]))
        if texts:
            return texts[-1][:2000]
        return super().extract_summary(stdout_tail)
