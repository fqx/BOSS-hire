"""Dependency smoke tests using synthetic data and loopback HTTP only."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest

import anyio
import httpx
from openai import OpenAI
import requests
import urllib3


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


class DependencyCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    async def _check_async_backend(self):
        results = []

        async def request():
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                response = await client.get(self.url)
                response.raise_for_status()
                results.append(response.json())

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(request)
            value = await anyio.to_thread.run_sync(lambda: 42)
        self.assertEqual(results, [{"status": "ok"}])
        self.assertEqual(value, 42)

    def test_anyio_httpx_asyncio_backend(self):
        anyio.run(self._check_async_backend, backend="asyncio")

    def test_anyio_httpx_trio_backend(self):
        anyio.run(self._check_async_backend, backend="trio")

    def test_requests_urllib3_transport(self):
        with requests.Session() as client:
            client.trust_env = False
            response = client.get(self.url, timeout=5)
            response.raise_for_status()
        self.assertEqual(response.json(), {"status": "ok"})

    def test_urllib3_transport(self):
        with urllib3.PoolManager() as client:
            response = client.request("GET", self.url, timeout=5, retries=False)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.json(), {"status": "ok"})

    def test_openai_client_with_mock_transport(self):
        def handle(request):
            self.assertEqual(request.url.path, "/v1/models")
            return httpx.Response(200, json={"object": "list", "data": []})

        with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
            with OpenAI(
                api_key="synthetic-test-key",
                base_url="https://example.invalid/v1",
                http_client=http_client,
                max_retries=0,
            ) as client:
                self.assertEqual(client.models.list().data, [])


if __name__ == "__main__":
    unittest.main()
