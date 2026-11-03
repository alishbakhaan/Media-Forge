"""Entry point of the packaged backend (PyInstaller).

`mediaforge-server --port N` starts the web server on 127.0.0.1:N.
`mediaforge-server -m <module> ...` runs a backend module the way `python -m` would; the image converter
starts its sandboxed workers like that, and inside a packaged app sys.executable is this program.
"""
import multiprocessing
import runpy
import sys


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "-m":
        module = sys.argv[2]
        sys.argv = [module, *sys.argv[3:]]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return
    port = 8000
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    import uvicorn
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
