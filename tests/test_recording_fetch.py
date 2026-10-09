"""A recording that is not on Africa's Talking's server yet is asked for again, a little later each time."""

import httpx
import pytest

from sofa.audio import fetch_recording


@pytest.fixture
def anyio_backend():
    return "asyncio"


def client(statuses):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[min(len(seen), len(statuses) - 1)]
        seen.append(status)
        return httpx.Response(status, content=b"AUDIO" if status == 200 else b"")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


@pytest.mark.anyio
async def test_a_recording_that_appears_after_two_misses_is_fetched():
    http, seen = client([404, 404, 200])
    assert await fetch_recording("https://x/y.mp3", wait=0, http=http) == b"AUDIO"
    assert seen == [404, 404, 200]


@pytest.mark.anyio
async def test_a_recording_that_never_appears_fails_after_the_attempts():
    http, seen = client([404])
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_recording("https://x/y.mp3", attempts=3, wait=0, http=http)
    assert len(seen) == 3


@pytest.mark.anyio
async def test_a_real_error_such_as_a_bad_request_is_not_retried():
    http, seen = client([400])
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_recording("https://x/y.mp3", wait=0, http=http)
    assert len(seen) == 1
