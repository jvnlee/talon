import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from talon.timeutil import now_utc

log = logging.getLogger(__name__)


def parallel_fetch[T](
    symbols: Sequence[str],
    fetch: Callable[[str], T],
    *,
    workers: int,
    max_failure_ratio: float,
    log_name: str,
    now: Callable[[], datetime] = now_utc,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[tuple[str, T, datetime]], int]:
    limit = len(symbols) * max_failure_ratio
    total = len(symbols)
    lock = threading.Lock()
    stop = threading.Event()
    results: dict[int, tuple[str, T, datetime]] = {}
    aborts: list[Exception] = []
    failed = 0
    done = 0

    def report(symbol: str) -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total, symbol)

    def work(index: int, symbol: str) -> None:
        nonlocal failed
        if stop.is_set():
            return
        try:
            value = fetch(symbol)
        except Exception as exc:
            with lock:
                failed += 1
                report(symbol)
                log.warning("%s fetch failed for %s: %s", log_name, symbol, exc)
                if failed > limit and not stop.is_set():
                    stop.set()
                    aborts.append(exc)
            return
        stamp = now()
        with lock:
            results[index] = (symbol, value, stamp)
            report(symbol)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for future in [pool.submit(work, index, symbol) for index, symbol in enumerate(symbols)]:
            future.result()
    if aborts:
        raise aborts[0]
    return [results[index] for index in sorted(results)], failed
