import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ratelimit import TokenBucket  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make(rate=1, capacity=3):
    c = FakeClock()
    return TokenBucket(rate, capacity, c), c


def test_starts_full():
    b, _ = make(capacity=3)
    assert [b.allow() for _ in range(3)] == [True, True, True]


def test_denies_when_empty():
    b, _ = make(capacity=2)
    b.allow()
    b.allow()
    assert b.allow() is False


def test_refills_over_time():
    b, c = make(rate=1, capacity=2)
    b.allow()
    b.allow()
    assert not b.allow()
    c.advance(1)
    assert b.allow() is True
    assert b.allow() is False


def test_partial_refill_not_enough():
    b, c = make(rate=1, capacity=1)
    b.allow()
    c.advance(0.5)
    assert b.allow() is False
    c.advance(0.5)
    assert b.allow() is True


def test_capped_at_capacity():
    b, c = make(rate=10, capacity=2)
    c.advance(1000)
    assert [b.allow() for _ in range(3)] == [True, True, False]


def test_rate_scales():
    b, c = make(rate=2, capacity=4)
    for _ in range(4):
        b.allow()
    c.advance(1)
    assert [b.allow() for _ in range(3)] == [True, True, False]


def test_clock_going_backwards_does_not_add_tokens():
    b, c = make(rate=1, capacity=1)
    b.allow()
    c.advance(-5)
    assert b.allow() is False
