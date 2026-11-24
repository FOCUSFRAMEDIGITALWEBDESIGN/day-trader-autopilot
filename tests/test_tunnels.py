"""Finding the quick-tunnel addresses (a fake cloudflared metrics endpoint and a fake dashboard)."""
import json, os, socket, sys, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import tunnels  # noqa: E402


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def serve(handler):
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


class Tunnels(unittest.TestCase):
    def test_hostnames_and_which_is_ours(self):
        dash_srv, dash_port = None, None

        class Dash(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                body = b"ok" if self.path == "/healthz" else b"other"
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

        class Other(Dash):
            def do_GET(self):
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

        dash_srv, dash_port = serve(Dash)
        other_srv, other_port = serve(Other)

        def metrics_for(host):
            class M(BaseHTTPRequestHandler):
                def log_message(self, *a): pass
                def do_GET(self):
                    body = json.dumps({"hostname": host}).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                    self.wfile.write(body)
            return M
        m1, p1 = serve(metrics_for("127.0.0.1:%d" % other_port))
        m2, p2 = serve(metrics_for("127.0.0.1:%d" % dash_port))
        try:
            hosts = tunnels.hostnames(ports=[p1, free_port(), p2])
            self.assertEqual(hosts, ["127.0.0.1:%d" % other_port, "127.0.0.1:%d" % dash_port])
            ours, others = tunnels.classify(hosts, scheme="http")
            self.assertEqual(ours, "127.0.0.1:%d" % dash_port)
            self.assertEqual(others, ["127.0.0.1:%d" % other_port])
        finally:
            for s in (dash_srv, other_srv, m1, m2):
                s.shutdown(); s.server_close()


if __name__ == "__main__":
    unittest.main()
