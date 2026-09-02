"""
Convenience launcher:  python run.py

`loop="none"` is load-bearing on Windows and must not be removed.

With reload enabled, uvicorn calls its own loop setup with use_subprocess=True,
and on Windows that swaps the event loop policy to WindowsSelectorEventLoopPolicy:

    def asyncio_setup(use_subprocess: bool = False) -> None:
        if sys.platform == "win32" and use_subprocess:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

Windows' SelectorEventLoop cannot spawn subprocesses. Our MCP knowledge server
*is* a subprocess (stdio transport), so the session fails to start with a bare
`NotImplementedError` and every classification returns 503.

`loop="none"` tells uvicorn to leave the event loop policy alone, so the default
WindowsProactorEventLoopPolicy stays in place and subprocesses work. Hot reload
is unaffected.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        loop="none",
    )
