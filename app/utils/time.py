import time


def now_ms() -> int:
    """Return the current time as epoch milliseconds."""
    return int(time.time() * 1000)
