"""Memory endpoint retries use AML rules, without network or sleeping."""
import importlib.util
import json
import unittest
from unittest.mock import patch

AVAILABLE = importlib.util.find_spec("aml_sdk") is not None and importlib.util.find_spec("pydantic") is not None
if AVAILABLE:
    import httpx
    from aml_sdk.client import ContractError, MemoryClient, MemoryHTTPError

ADD = {"request_id": "logical-write", "user_id": "u", "session_id": "s",
       "messages": [{"role": "user", "content": "synthetic fixture"}]}
SEARCH = {"user_id": "u", "query": "synthetic question", "top_k": 100}


@unittest.skipUnless(AVAILABLE, "Requires the optional AML HTTP client and pydantic")
class MemoryEndpointRetryTests(unittest.TestCase):
    def test_http_bridge_returns_failures_without_multiplying_retries(self):
        from aml_sdk.backend import BackendError, HTTPBackend
        calls = []
        backend = HTTPBackend({"base_url": "http://memory.invalid", "auth_scheme": "none"})
        self.addCleanup(backend.close)
        backend.client.http.close()
        def respond(request):
            calls.append(request)
            return httpx.Response(429)
        backend.client.http = httpx.Client(transport=httpx.MockTransport(respond))
        with self.assertRaises(BackendError) as error:
            backend.add(ADD)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(len(calls), 1)

    def client(self, handler, **options):
        sleeps = []
        client = MemoryClient("https://memory.invalid", auth_scheme="none",
                              transport=httpx.MockTransport(handler), sleep=sleeps.append, **options)
        self.addCleanup(client.close)
        return client, sleeps

    def test_all_retry_statuses_preserve_exact_bytes_and_identifiers(self):
        for operation, statuses in (("add", [408, 409, 425, 429, 500, 502, 503, 504, 524]),
                                    ("search", [408, 425, 429, 500, 502, 503, 504])):
            for status in statuses:
                with self.subTest(operation=operation, status=status):
                    calls = []
                    def respond(request):
                        calls.append(request.content)
                        return httpx.Response(status, headers={"Retry-After": "3"}) if len(calls) == 1 else httpx.Response(
                            200, json={"success": True, **{k: ADD[k] for k in ("request_id", "user_id", "session_id")}}
                            if operation == "add" else {"data": []})
                    client, sleeps = self.client(respond)
                    getattr(client, operation)(ADD if operation == "add" else SEARCH)
                    self.assertEqual(calls[0], calls[1])
                    self.assertEqual(sleeps, [3 if status == 429 else 1])

    def test_permanent_errors_and_bad_200_bodies_stop_immediately(self):
        for operation, status in [(op, s) for op in ("add", "search") for s in (400, 401, 403, 404, 422, 302)] + [
                ("search", 409), ("search", 524)]:
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(status)
            client, sleeps = self.client(respond)
            with self.subTest(operation=operation, status=status), self.assertRaises(MemoryHTTPError):
                getattr(client, operation)(ADD if operation == "add" else SEARCH)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sleeps, [])
        for operation, body in [("add", {"success": "false"}), ("add", {"success": True,
                "request_id": "wrong", "user_id": "u", "session_id": "s"}), ("search", {"data": [{}]})]:
            client, sleeps = self.client(lambda request: httpx.Response(200, json=body))
            with self.subTest(body=body), self.assertRaises(ContractError):
                getattr(client, operation)(ADD if operation == "add" else SEARCH)
            self.assertEqual(sleeps, [])

    def test_transport_errors_replay_same_write_and_stop_at_bound(self):
        calls = []
        def respond(request):
            calls.append(request.content)
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        client, sleeps = self.client(respond, max_attempts=3)
        with self.assertRaises(httpx.TransportError):
            client.add(ADD)
        self.assertEqual(calls, [calls[0]] * 3)
        self.assertEqual(sleeps, [1, 2])

    def test_32_attempt_ceiling_and_bounded_backoff(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(503)
        client, sleeps = self.client(respond)
        with self.assertRaises(MemoryHTTPError):
            client.add(ADD)
        self.assertEqual(len(calls), 32)
        self.assertEqual(len(sleeps), 31)
        self.assertEqual(sleeps[:7], [1, 2, 4, 8, 16, 32, 60])
        self.assertEqual(max(sleeps), 60)
        for value in (0, 33, True, 1.5):
            with self.assertRaises(ValueError):
                MemoryClient("https://memory.invalid", auth_scheme="none", max_attempts=value)

    def test_429_retry_after_fallback_clamp_and_http_date(self):
        for value, expected in [(None, 60), ("bad", 60), ("-2", 60), ("1.5", 60), ("0", 0), ("7", 7), ("120", 60),
                                ("Wed, 21 Oct 2015 07:28:00 GMT", 0)]:
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(429, headers={} if value is None else {"Retry-After": value}) if len(calls) == 1 else httpx.Response(200, json={"data": []})
            client, sleeps = self.client(respond)
            client.search(SEARCH)
            self.assertEqual(sleeps, [expected])


if __name__ == "__main__":
    unittest.main()
