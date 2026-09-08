"""Child side of the spawn handshake (see process.run_process).

The backend spawns every provider/verification child through this shim with
an inherited gate file descriptor. The shim blocks reading a single release
byte:

- ``b"G"`` → exec the real program. The parent writes it only AFTER the
  child's verified identity (pid/pgid/start time) is durably persisted.
- anything else, including EOF → exit 42 without executing anything. The
  kernel closes the parent's pipe ends on process death, so a backend that
  dies at any point before release deterministically yields EOF: the child
  can never begin task work without a persisted identity.

Usage: <python> _spawn_gate.py <gate_fd> <argv...>
"""

from __future__ import annotations

import os
import sys

GATE_RELEASE_BYTE = b"G"
GATE_REFUSED_EXIT = 42


def main() -> int:
    fd = int(sys.argv[1])
    prog = sys.argv[2:]
    try:
        data = os.read(fd, 1)
    except OSError:
        data = b""
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if data == GATE_RELEASE_BYTE and prog:
        os.execvp(prog[0], prog)  # noqa: S606 - argv array, never a shell
        return 127  # pragma: no cover - execvp only returns on failure
    return GATE_REFUSED_EXIT


if __name__ == "__main__":
    sys.exit(main())
