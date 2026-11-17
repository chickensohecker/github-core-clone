"""HTTP server, routing and the trial lifecycle.

One process: binds an ephemeral port, registers it with the suite, serves
git smart HTTP + REST v3 + GraphQL + MCP from one in-memory store, long-polls
/done and exits 0 when the suite is finished.
"""

import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import gitpack, graphql, mcp, rest
from . import githttp
from .store import ForgeError, Store
from . import serial

JSON = "application/json"


class ForgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address, handler, store):
        super().__init__(address, handler)
        self.store = store


class ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "github-core-clone/1.0.0"

    def setup(self):
        super().setup()
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, *args):
        pass

    # -- verbs ---------------------------------------------------------

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    # -- plumbing ------------------------------------------------------

    def _send(self, status, content_type, body, extra=None):
        reason = {200: "OK", 201: "Created", 202: "Accepted", 204: "No Content",
                  400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
                  405: "Method Not Allowed", 409: "Conflict",
                  422: "Unprocessable Entity", 500: "Internal Server Error"}.get(status, "OK")
        head = []
        head.append(("HTTP/1.1 %d %s\r\n" % (status, reason)).encode())
        head.append(("Content-Type: %s\r\n" % content_type).encode())
        head.append(("Content-Length: %d\r\n" % len(body)).encode())
        head.append(("X-GitHub-Request-Id: %s\r\n" % self.server.store.next_request_id()).encode())
        for key, value in (extra or {}).items():
            head.append(("%s: %s\r\n" % (key, value)).encode())
        head.append(b"\r\n")
        try:
            self.wfile.write(b"".join(head) + body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _unauthorized(self):
        self._send(401, JSON, serial.dumps({"message": "Unauthorized"}))

    def _handle(self):
        try:
            self._route()
        except Exception as exc:  # never let one request kill the server
            try:
                self._send(500, JSON, serial.dumps(
                    {"message": "internal error: %s" % exc}))
            except Exception:
                self.close_connection = True

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _authorized(self):
        want = "Bearer " + self.server.store.token
        return self.headers.get("Authorization") == want

    def _route(self):
        split = urllib.parse.urlsplit(self.path)
        path = split.path
        query = urllib.parse.parse_qs(split.query)
        segments = [urllib.parse.unquote(part)
                    for part in path.strip("/").split("/") if part != ""]
        store = self.server.store
        host = self.headers.get("Host") or "127.0.0.1"
        method = self.command
        # Always consume the request body first: answering (for example a
        # 401) while bytes are still unread would leave them in the socket
        # buffer, where the next request on a keep-alive connection would be
        # parsed from them and fail.
        body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""

        if path == "/healthz" and method == "GET":
            self._send(200, JSON, b'{"ok": true}')
            return
        if path == "/healthz":
            self._send(404, JSON, serial.dumps({"message": "Not Found"}))
            return

        if not self._authorized():
            self._unauthorized()
            return

        if segments == ["mcp"]:
            if method != "POST":
                self._send(404, JSON, serial.dumps({"message": "Not Found"}))
                return
            status, ctype, out = mcp.handle(store, body)
            self._send(status, ctype, out)
            return

        if len(segments) >= 2 and segments[0] == "api" and segments[1] == "v3":
            status, ctype, out, extra = rest.handle(
                store, method, segments[2:], query, self.headers,
                body, host)
            self._send(status, ctype, out, extra)
            return

        if segments == ["api", "graphql"]:
            if method != "POST":
                self._send(404, JSON, serial.dumps({"message": "Not Found"}))
                return
            status, ctype, out = graphql.handle(store, body)
            self._send(status, ctype, out)
            return

        if len(segments) == 4 and segments[1].endswith(".git") \
                and segments[2] == "info" and segments[3] == "refs" and method == "GET":
            owner, repo_name = segments[0], segments[1][:-4]
            service = query.get("service", [None])[0]
            git_protocol = self.headers.get("Git-Protocol")
            try:
                status, ctype, out = githttp.info_refs(
                    store, owner, repo_name, service, git_protocol)
            except ForgeError as exc:
                self._send(exc.status, JSON, serial.dumps({"message": exc.message}))
                return
            self._send(status, ctype, out)
            return

        if len(segments) == 3 and segments[1].endswith(".git") and method == "POST":
            owner, repo_name = segments[0], segments[1][:-4]
            service = segments[2]
            try:
                if service == "git-upload-pack":
                    git_protocol = self.headers.get("Git-Protocol")
                    status, ctype, out = githttp.upload_pack(
                        store, owner, repo_name, git_protocol, body)
                elif service == "git-receive-pack":
                    status, ctype, out = githttp.receive_pack(
                        store, owner, repo_name, body)
                else:
                    self._send(404, JSON, serial.dumps({"message": "Not Found"}))
                    return
            except ForgeError as exc:
                self._send(exc.status, JSON, serial.dumps({"message": exc.message}))
                return
            self._send(status, ctype, out)
            return

        self._send(404, JSON, serial.dumps({"message": "Not Found"}))


# ---------------------------------------------------------------- lifecycle

def _post_register(env_url, port):
    url = env_url.rstrip("/") + "/register"
    data = json.dumps({"port": port}).encode()
    while True:
        try:
            request = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                if 200 <= response.status < 300:
                    return
        except Exception:
            pass
        time.sleep(2)


def _wait_done(env_url):
    url = env_url.rstrip("/") + "/done"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = json.loads(response.read().decode())
            if payload.get("done") is True:
                return
        except Exception:
            pass
        time.sleep(0.5)


def run_sealed(store, env_url):
    server = ForgeServer(("0.0.0.0", 0), ForgeHandler, store)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    threading.Thread(target=_post_register, args=(env_url, port),
                     daemon=True).start()
    _wait_done(env_url)
    server.shutdown()
    server.server_close()


def run_dev(store, host, port):
    server = ForgeServer((host, port), ForgeHandler, store)
    print("forge listening on http://%s:%d (token required)" % (host, port),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    token = os.environ.get("MAKEFUN_SHOP_TOKEN") or "test-token"
    store = Store(token)
    env_url = os.environ.get("MAKEFUN_ENV_URL")
    if env_url:
        run_sealed(store, env_url)
    else:
        port = int(os.environ.get("FORGE_PORT", "18741"))
        run_dev(store, "127.0.0.1", port)


if __name__ == "__main__":
    main()
