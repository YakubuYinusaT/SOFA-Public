"""One broken connection to the GPU host must not cost a caller a turn."""

import asyncio
import ssl

import httpx
import pytest

from sofa.clients.http import RetryingClient


def client(handler, retries=2):
    return RetryingClient(transport=httpx.MockTransport(handler), retries=retries)


def run(coro):
    return asyncio.run(coro)


def test_a_broken_connection_is_tried_again_and_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadError("connection garbled")
        return httpx.Response(200, json={"ok": True})

    r = run(client(handler).post("http://gpu/x", json={}))
    assert r.json() == {"ok": True} and len(calls) == 3


def test_an_ssl_error_is_retried_too():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise ssl.SSLError("sslv3 alert bad record mac")
        return httpx.Response(200)

    assert run(client(handler).post("http://gpu/x")).status_code == 200 and len(calls) == 2


def test_it_gives_up_after_the_retries_and_says_why():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        run(client(handler, retries=2).post("http://gpu/x"))
    assert len(calls) == 3


def test_an_error_answer_from_the_host_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    assert run(client(handler).post("http://gpu/x")).status_code == 500 and len(calls) == 1


def test_a_timeout_is_not_retried_because_the_caller_would_wait_twice():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(httpx.ReadTimeout):
        run(client(handler).post("http://gpu/x"))
    assert len(calls) == 1


def test_connections_to_the_gpu_host_use_tls_1_2_at_most(monkeypatch):
    import ssl

    from sofa.clients import http

    monkeypatch.delenv("GPU_TLS12", raising=False)
    assert http._tls_context().maximum_version == ssl.TLSVersion.TLSv1_2
    monkeypatch.setenv("GPU_TLS12", "0")
    assert http._tls_context().maximum_version != ssl.TLSVersion.TLSv1_2
