"""Codex CLI adapter — verified against codex-cli 0.153.1.

Non-interactive: `codex exec --json --sandbox workspace-write
--skip-git-repo-check -C <workdir> <prompt>`
`--json` emits JSONL events; agent_message items carry readable text.
"""

from __future__ import annotations

from .base import (
    ExecutionRequest,
    ProviderAdapter,
    ProviderCapabilities,
    extract_json_line,
)


class CodexAdapter(ProviderAdapter):
    name = "codex"
    executable = "codex"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming_json=True, supports_cwd_flag=True)

    def build_command(self, request: ExecutionRequest) -> list[str]:
        return [
            self.executable, "exec",
            "--json",
            "--sandbox", "workspace-write",
            "--skip-git-repo-check",
            "--color", "never",
            "-C", str(request.workdir),
            request.prompt,
        ]

    def normalize_output_line(self, line: str) -> str | None:
        event = extract_json_line(line)
        if event is None:
            return line or None
        etype = event.get("type") or event.get("msg", {}).get("type")
        if etype in ("agent_message", "item.completed"):
            item = event.get("item") or event.get("msg") or event
            text = item.get("text") or item.get("message")
            if text:
                return str(text)
        if etype == "exec_command_begin":
            cmd = (event.get("msg") or {}).get("command")
            return f"[exec: {cmd}]" if cmd else None
        if etype == "error":
            return f"[codex error] {event.get('msg', event)}"
        return None

    def extract_summary(self, stdout_tail: list[str]) -> str:
        for line in reversed(stdout_tail):
            event = extract_json_line(line)
            if event:
                item = event.get("item") or event.get("msg") or {}
                text = item.get("text") or item.get("message")
                if text:
                    return str(text)[:2000]
        return super().extract_summary(stdout_tail)
