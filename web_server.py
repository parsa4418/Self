from http.server import HTTPServer, BaseHTTPRequestHandler
import os
import threading


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"SelfBot is running")


def start_web_server():
    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(("0.0.0.0", port), Handler)

    print(f"Web server running on port {port}")

    server.serve_forever()


if __name__ == "__main__":
    start_web_server()
