"""OpenCode CLI adapter — verified against opencode 1.17.13.

Non-interactive: `opencode run --format json --auto --dir <workdir> <message>`
`--format json` emits JSON events with `type`/`part` structure.

IMPORTANT (runtime-verified 2026-09): opencode's interactive default model may
hang indefinitely in headless `run` mode. The adapter pins a configurable model
(`providers.opencode.model`, default set in config/orchestrator.yaml) that is
verified to respond non-interactively.
"""

from __future__ import annotations

from .base import ExecutionRequest, ProviderAdapter, ProviderCapabilities, extract_json_line


class OpencodeAdapter(ProviderAdapter):
    name = "opencode"
    executable = "opencode"

    def __init__(self, executable: str | None = None, model: str | None = None):
        super().__init__(executable)
        self.model = model

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming_json=True, supports_cwd_flag=True)

    def build_command(self, request: ExecutionRequest) -> list[str]:
        argv = [
            self.executable,
            "run",
            "--format", "json",
            "--auto",
            "--dir", str(request.workdir),
        ]
        if self.model:
            argv += ["-m", self.model]
        argv.append(request.prompt)
        return argv

    def normalize_output_line(self, line: str) -> str | None:
        event = extract_json_line(line)
        if event is None:
            return line or None
        part = event.get("part", {})
        if isinstance(part, dict):
            ptype = part.get("type")
            if ptype == "text" and part.get("text"):
                return str(part["text"])
            if ptype == "tool":
                return f"[tool: {part.get('tool', '?')}]"
        if event.get("type") == "error":
            return f"[opencode error] {event}"
        return None

    def extract_assistant_text(self, stdout_tail: list[str]) -> str:
        """Reconstruct assistant response from OpenCode JSON events."""
        parts: list[str] = []
        for line in stdout_tail:
            event = extract_json_line(line)
            if not event:
                continue
            part = event.get("part", {})
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                parts.append(str(part["text"]))
        return "\n".join(parts)

    def extract_summary(self, stdout_tail: list[str]) -> str:
        texts: list[str] = []
        for line in stdout_tail:
            event = extract_json_line(line)
            if event:
                part = event.get("part")
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    texts.append(str(part["text"]))
        if texts:
            return texts[-1][:2000]
        return super().extract_summary(stdout_tail)

    def is_success_marker(self, text: str) -> bool:
        normalized = text.replace(" ", "")
        return (
            '"type":"finish"' in normalized
            or '"type":"done"' in normalized
            or '"step_finish"' in normalized
        )
