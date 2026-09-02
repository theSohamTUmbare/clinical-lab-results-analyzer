"""
Convenience launcher:  python run.py

Two Windows-specific hazards are handled here, both of which cost real debugging
time during development.

1. The interpreter must be the project's virtualenv.

   Dependencies are installed into backend/.venv. If this file is run with a
   different Python that happens to be first on PATH - Anaconda is the usual
   culprit - `import mcp` fails, the app cannot start, and the reloader can
   leave a socket bound to port 8000 that answers requests with 500 while the
   real error scrolls past in another window. We check up front and say so.

2. `loop="none"` is load-bearing and must not be removed.

   With reload enabled, uvicorn calls its own loop setup with
   use_subprocess=True, and on Windows that swaps the event loop policy:

       def asyncio_setup(use_subprocess: bool = False) -> None:
           if sys.platform == "win32" and use_subprocess:
               asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

   Windows' SelectorEventLoop cannot spawn subprocesses. The MCP knowledge
   server *is* a subprocess (stdio transport), so the session fails to start
   with a bare NotImplementedError and every classification returns 503.
   `loop="none"` leaves the default Proactor policy in place. Hot reload is
   unaffected.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent


def venv_python() -> Path:
    """Path to the project virtualenv's interpreter for this platform."""
    if sys.platform == "win32":
        return BACKEND_ROOT / ".venv" / "Scripts" / "python.exe"
    return BACKEND_ROOT / ".venv" / "bin" / "python"


def check_interpreter() -> None:
    """Refuse to start on an interpreter that cannot import the dependencies."""
    if importlib.util.find_spec("mcp") is not None:
        return

    expected = venv_python()
    print("ERROR: this Python cannot import 'mcp', so the app cannot start.\n", file=sys.stderr)
    print(f"  running with : {sys.executable}", file=sys.stderr)
    print(f"  expected     : {expected}\n", file=sys.stderr)

    if expected.exists():
        print("The dependencies are installed in the project virtualenv, not in this", file=sys.stderr)
        print("interpreter. Start the backend with it explicitly:\n", file=sys.stderr)
        rel = expected.relative_to(BACKEND_ROOT) if expected.is_relative_to(BACKEND_ROOT) else expected
        print(f"  {rel} run.py\n", file=sys.stderr)
    else:
        print("The project virtualenv does not exist yet. Create it and install:\n", file=sys.stderr)
        print("  python -m venv .venv", file=sys.stderr)
        print("  .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    check_interpreter()

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        loop="none",
    )
