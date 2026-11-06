import asyncio

from app.integrations.ratelimit import RateLimiter, get_limiter
from tests.mbdata import FakeTime


def make(interval=1.1):
    ft = FakeTime()
    return RateLimiter(interval, clock=ft.clock, sleep=ft.sleep), ft


async def test_first_call_is_immediate_then_spaced():
    limiter, ft = make()
    for _ in range(3):
        await limiter.wait()
    assert ft.sleeps == [1.1, 1.1] or [round(s, 6) for s in ft.sleeps] == [1.1, 1.1]


async def test_no_wait_when_enough_time_has_passed():
    limiter, ft = make()
    await limiter.wait()
    ft.t += 5
    await limiter.wait()
    assert ft.sleeps == []


async def test_concurrent_callers_are_serialized():
    limiter, ft = make()
    await asyncio.gather(*(limiter.wait() for _ in range(4)))
    assert len(ft.sleeps) == 3 and sum(ft.sleeps) >= 3.3 - 1e-9


async def test_penalty_delays_the_next_call():
    limiter, ft = make()
    await limiter.wait()
    limiter.penalize(10)
    await limiter.wait()
    assert ft.sleeps and ft.sleeps[0] >= 10 - 1e-9


async def test_penalty_never_shortens_a_longer_wait():
    limiter, ft = make()
    limiter.penalize(30)
    limiter.penalize(5)
    await limiter.wait()
    assert ft.sleeps[0] >= 30 - 1e-9


def test_limiters_are_shared_per_service():
    first = get_limiter("test-service", 1.0)
    assert get_limiter("test-service", 9.0) is first and first.min_interval == 9.0
    assert get_limiter("test-service", 1.0) is not get_limiter("other-service", 1.0)
