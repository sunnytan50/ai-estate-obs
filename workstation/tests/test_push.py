"""TDD test suite for aiobs_collector.push (Task 11: push to VictoriaMetrics).

Runnable with no environment variables, from either location:
    python3 -m unittest discover -s workstation/tests -v   # from repo root
    python3 -m unittest discover -s tests -v                # from workstation/

Two complementary strategies, per the controller resolution:
  - A real stdlib `http.server` stub on 127.0.0.1 (ephemeral port) verifies
    actual request/response behavior end-to-end: path, body, non-2xx ->
    raise. No mocking of urllib itself in these tests.
  - A handful of `unittest.mock`-patched tests (same style as
    test_lane_openrouter.py's header/timeout assertions) pin exact request
    construction (method, URL join, timeout=60) that would be awkward to
    observe reliably through a real socket.
"""

import http.server
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

# workstation/ (this file's grandparent) holds the aiobs_collector package.
_WORKSTATION_DIR = str(Path(__file__).resolve().parent.parent)
if _WORKSTATION_DIR not in sys.path:
    sys.path.insert(0, _WORKSTATION_DIR)

from aiobs_collector.core import Sample, render_exposition  # noqa: E402
from aiobs_collector.push import push_samples  # noqa: E402


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Records the one request it receives on `self.server`; replies with
    whatever status/body the server was configured with. State lives on the
    server object (not the handler, which is re-instantiated per request)
    so the test can inspect it after the client call returns.
    """

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.server.received_path = self.path
        self.server.received_body = body
        self.server.received_content_type = self.headers.get("Content-Type")
        self.send_response(self.server.reply_status)
        self.send_header("Content-Length", str(len(self.server.reply_body)))
        self.end_headers()
        self.wfile.write(self.server.reply_body)

    def do_GET(self):  # pragma: no cover - only exercised if a bug sends GET
        self.server.received_path = self.path
        self.server.received_body = b""
        self.send_response(405)
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # silence request logging to stderr during test runs


class _StubServer(http.server.HTTPServer):
    def __init__(self, reply_status=204, reply_body=b""):
        super().__init__(("127.0.0.1", 0), _RecordingHandler)
        self.reply_status = reply_status
        self.reply_body = reply_body
        self.received_path = None
        self.received_body = None
        self.received_content_type = None


class _StubServerTestCase(unittest.TestCase):
    """Base class: starts a fresh stub server per test in a daemon thread,
    torn down via addCleanup. `self.base_url` is the server's http://127.0.0.1:PORT.
    """

    def _start_server(self, reply_status=204, reply_body=b""):
        server = _StubServer(reply_status=reply_status, reply_body=reply_body)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, timeout=5)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        return server, f"http://127.0.0.1:{port}"


class PushSamplesHttpStubTests(_StubServerTestCase):
    def setUp(self):
        self.samples = [
            Sample(
                metric="aiobs_tokens_total",
                labels={"provider": "claude-code", "model": "opus", "kind": "input", "origin": "client"},
                value=123.0,
                ts_ms=1756400000000,
            ),
            Sample(metric="aiobs_lane_up", labels={"lane": "tokscale"}, value=1.0, ts_ms=1756400000000),
        ]

    def test_posts_to_import_prometheus_path(self):
        server, base_url = self._start_server(reply_status=204, reply_body=b"")
        push_samples(base_url, self.samples)
        self.assertEqual(server.received_path, "/api/v1/import/prometheus")

    def test_body_matches_render_exposition_output(self):
        server, base_url = self._start_server(reply_status=204, reply_body=b"")
        push_samples(base_url, self.samples)
        self.assertEqual(server.received_body.decode("utf-8"), render_exposition(self.samples))

    def test_success_204_no_content_does_not_raise(self):
        _server, base_url = self._start_server(reply_status=204, reply_body=b"")
        push_samples(base_url, self.samples)  # must not raise

    def test_success_200_does_not_raise(self):
        _server, base_url = self._start_server(reply_status=200, reply_body=b"ok")
        push_samples(base_url, self.samples)  # must not raise

    def test_empty_samples_list_posts_empty_body(self):
        server, base_url = self._start_server(reply_status=204, reply_body=b"")
        push_samples(base_url, [])
        self.assertEqual(server.received_path, "/api/v1/import/prometheus")
        self.assertEqual(server.received_body, b"")

    def test_non_2xx_raises_runtime_error(self):
        _server, base_url = self._start_server(reply_status=500, reply_body=b"boom")
        with self.assertRaises(RuntimeError):
            push_samples(base_url, self.samples)

    def test_non_2xx_message_contains_status_code(self):
        _server, base_url = self._start_server(reply_status=400, reply_body=b"bad request detail")
        with self.assertRaisesRegex(RuntimeError, "400"):
            push_samples(base_url, self.samples)

    def test_non_2xx_message_contains_body_prefix(self):
        _server, base_url = self._start_server(reply_status=500, reply_body=b"exact error text here")
        with self.assertRaisesRegex(RuntimeError, "exact error text here"):
            push_samples(base_url, self.samples)

    def test_non_2xx_message_body_prefix_capped_at_200_chars(self):
        long_body = ("x" * 500).encode("utf-8")
        _server, base_url = self._start_server(reply_status=503, reply_body=long_body)
        with self.assertRaises(RuntimeError) as ctx:
            push_samples(base_url, self.samples)
        message = str(ctx.exception)
        # The full 500-char run of x's must not appear verbatim; at most a
        # 200-char slice of it may.
        self.assertNotIn("x" * 201, message)
        self.assertIn("x" * 200, message)

    def test_trailing_slash_in_base_url_does_not_double_slash_path(self):
        server, base_url = self._start_server(reply_status=204, reply_body=b"")
        push_samples(base_url + "/", self.samples)
        self.assertEqual(server.received_path, "/api/v1/import/prometheus")


class PushSamplesMockedRequestTests(unittest.TestCase):
    """Pins exact Request construction (method, URL, body bytes, timeout)
    via mocking, the same pattern test_lane_openrouter.py uses for its
    Authorization-header assertion -- these properties are awkward to
    observe reliably through a real socket.
    """

    def _mock_response(self):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.status = 204
        return resp

    def test_uses_post_method_and_60s_timeout(self):
        samples = [Sample(metric="m", labels={}, value=1.0, ts_ms=1)]
        with patch(
            "aiobs_collector.push.urllib.request.urlopen", return_value=self._mock_response()
        ) as mock_urlopen:
            push_samples("http://example.invalid:8428", samples)

        mock_urlopen.assert_called_once()
        request_obj = mock_urlopen.call_args[0][0]
        self.assertEqual(request_obj.get_method(), "POST")
        self.assertEqual(
            request_obj.full_url, "http://example.invalid:8428/api/v1/import/prometheus"
        )
        self.assertEqual(request_obj.data, render_exposition(samples).encode("utf-8"))
        self.assertEqual(mock_urlopen.call_args.kwargs.get("timeout"), 60)

    def test_url_join_strips_exactly_one_trailing_slash(self):
        samples = [Sample(metric="m", labels={}, value=1.0, ts_ms=1)]
        with patch(
            "aiobs_collector.push.urllib.request.urlopen", return_value=self._mock_response()
        ) as mock_urlopen:
            push_samples("http://example.invalid:8428/", samples)
        request_obj = mock_urlopen.call_args[0][0]
        self.assertEqual(
            request_obj.full_url, "http://example.invalid:8428/api/v1/import/prometheus"
        )

    def test_url_error_wrapped_as_runtime_error(self):
        samples = [Sample(metric="m", labels={}, value=1.0, ts_ms=1)]
        with patch(
            "aiobs_collector.push.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaisesRegex(RuntimeError, "connection refused"):
                push_samples("http://example.invalid:8428", samples)

    def test_http_error_reason_never_needed_status_and_body_used_instead(self):
        # HTTPError IS a URLError subclass; confirm the HTTPError branch
        # (status + body) is what fires, not the generic URLError branch.
        samples = [Sample(metric="m", labels={}, value=1.0, ts_ms=1)]
        http_error = urllib.error.HTTPError(
            url="http://example.invalid:8428/api/v1/import/prometheus",
            code=422,
            msg="Unprocessable",
            hdrs=None,
            fp=None,
        )
        # HTTPError.read() needs a real/fake file-like fp; patch it directly.
        http_error.read = MagicMock(return_value=b"unprocessable detail")
        with patch(
            "aiobs_collector.push.urllib.request.urlopen", side_effect=http_error
        ):
            with self.assertRaisesRegex(RuntimeError, "422"):
                push_samples("http://example.invalid:8428", samples)


if __name__ == "__main__":
    unittest.main()
