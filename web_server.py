from http.server import HTTPServer, BaseHTTPRequestHandler
import http.client
import os


class Handler(BaseHTTPRequestHandler):
    """Public Render HTTP endpoint.

    Render owns PORT. The Telegram webhook application runs on a private
    localhost port, and requests to /telegram are transparently proxied to it.
    The root endpoint stays available for Render/browser health checks.
    """

    def _send_ok(self):
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Keep the public root endpoint simple and independent of Telegram.
        if self.path.split("?", 1)[0].rstrip("/") == "":
            self._send_ok()
            return
        self.send_error(404, "Not Found")

    def do_HEAD(self):
        if self.path.split("?", 1)[0].rstrip("/") == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.end_headers()
            return
        self.send_error(404, "Not Found")

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        webhook_path = os.getenv("WEBHOOK_PATH", "telegram").strip("/")

        if path != f"/{webhook_path}":
            self.send_error(404, "Not Found")
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        body = self.rfile.read(content_length) if content_length else b""

        internal_port = int(os.getenv("INTERNAL_WEBHOOK_PORT", "10001"))
        headers = {}
        for name in ("Content-Type", "Content-Length", "X-Telegram-Bot-Api-Secret-Token"):
            value = self.headers.get(name)
            if value is not None:
                headers[name] = value

        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", internal_port, timeout=30
            )
            connection.request("POST", f"/{webhook_path}", body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            status = response.status
            response_type = response.getheader("Content-Type")
            connection.close()

            self.send_response(status)
            if response_type:
                self.send_header("Content-Type", response_type)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
        except Exception as exc:
            print(f"Webhook proxy error: {exc}")
            self.send_error(502, "Webhook backend unavailable")

    def log_message(self, fmt, *args):
        print(f"WebServer: {self.address_string()} - {fmt % args}")


def start_web_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Public web server running on port {port}")
    print(
        "Webhook proxy target: "
        f"127.0.0.1:{os.getenv('INTERNAL_WEBHOOK_PORT', '10001')}"
    )
    server.serve_forever()


if __name__ == "__main__":
    start_web_server()
