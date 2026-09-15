"""Start the built React frontend and the existing FastAPI backend together."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import json
import os
import socket

import uvicorn


class FrontendHandler(SimpleHTTPRequestHandler):
    api_url = "http://127.0.0.1:8000"
    data_formulator_url = os.getenv("DATA_FORMULATOR_URL", "http://localhost:5567")
    presenton_url = os.getenv("PRESENTON_URL", "http://localhost:5001")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/runtime-config.js":
            body = (
                f"window.API_BASE_URL = {json.dumps(self.api_url)};\n"
                f"window.DATA_FORMULATOR_URL = {json.dumps(self.data_formulator_url)};\n"
                f"window.PRESENTON_URL = {json.dumps(self.presenton_url)};\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def bind_backend():
    for port in range(8000, 8021):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            sock.set_inheritable(True)
            return sock
        except OSError:
            sock.close()
    raise SystemExit("No available backend port between 8000 and 8020.")


def main():
    root = Path(__file__).resolve().parent
    frontend_dist = root / "frontend" / "dist"
    if not (frontend_dist / "index.html").is_file():
        raise SystemExit("React frontend is not built. Run: cd frontend && npm install && npm run build")

    backend_socket = bind_backend()
    port = backend_socket.getsockname()[1]
    FrontendHandler.api_url = f"http://127.0.0.1:{port}"
    handler = partial(FrontendHandler, directory=str(frontend_dist))

    try:
        frontend = ThreadingHTTPServer(("127.0.0.1", 3000), handler)
    except OSError as exc:
        backend_socket.close()
        raise SystemExit(f"Cannot start frontend on port 3000: {exc}") from exc

    worker = Thread(target=frontend.serve_forever, daemon=True)
    worker.start()
    print("Application: http://localhost:3000", flush=True)
    print(f"API docs: http://localhost:{port}/docs", flush=True)
    print("Press Ctrl+C to stop both servers.", flush=True)

    try:
        config = uvicorn.Config(
            "backend.main:app",
            host="127.0.0.1",
            port=port,
            reload=True,
            reload_dirs=[str(root / "backend")],
        )
        from uvicorn.supervisors import ChangeReload
        server = uvicorn.Server(config)
        ChangeReload(config, target=server.run, sockets=[backend_socket]).run()
    finally:
        frontend.shutdown()
        frontend.server_close()
        worker.join()
        backend_socket.close()


if __name__ == "__main__":
    main()
