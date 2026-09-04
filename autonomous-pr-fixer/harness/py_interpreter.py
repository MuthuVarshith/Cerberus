"""
Portable Python interpreter resolution.

Agents build shell command strings such as ``"<python> -m pytest test_reproduce.py"``
and hand them to :meth:`Sandbox.exec`. Hardcoding a launcher (``py -3.13``) binds
the harness to Windows: on macOS, Linux, or a CI runner every such command fails
with "command not found", which silently turns real test results into false
negatives. The interpreter token is therefore resolved once, here.

Two different tokens are needed depending on where the command will run:

- ``HOST_PYTHON`` — the interpreter running this process, for the process-fallback
  sandbox. Quoted when the path contains spaces (e.g. ``C:\\Program Files\\...``).
- ``CONTAINER_PYTHON`` — a bare name, for Docker mode. A host absolute path is
  meaningless inside ``python:3.11-slim``.

Prefer :attr:`Sandbox.python_cmd`, which picks the correct one for that sandbox.
"""
from __future__ import annotations

import shutil
import sys

#: Interpreter name inside a container image.
CONTAINER_PYTHON = "python"


def host_python() -> str:
    """Return a shell-safe token invoking the interpreter running this process."""
    exe = sys.executable
    if not exe:
        # Embedded or frozen interpreters may leave sys.executable empty.
        exe = shutil.which("python3") or shutil.which("python") or "python3"
    return f'"{exe}"' if " " in exe else exe


#: Resolved once at import; the running interpreter cannot change mid-process.
HOST_PYTHON = host_python()
