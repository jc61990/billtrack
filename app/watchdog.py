"""Freeze detector. If the event loop stops responding, or every worker thread stays busy, for 20 s, dump the
stack of every thread to the journal so the cause is visible. `systemctl kill -s USR1 billtrack` dumps on demand."""
import faulthandler
import logging
import signal
import sys
import threading
import time

log = logging.getLogger("billtrack.watchdog")
STALL_S = int(__import__("os").getenv("BT_WATCHDOG_STALL_S", "20"))


def start(loop, limiter=None):
    try:
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
    except (AttributeError, ValueError, RuntimeError):
        pass
    last = [time.monotonic()]

    def beat():
        last[0] = time.monotonic()

    def run():
        dumped, busy_since = False, None
        while True:
            time.sleep(5)
            try:
                loop.call_soon_threadsafe(beat)
            except RuntimeError:   # loop closed (shutdown)
                return
            now = time.monotonic()
            stall = now - last[0]
            saturated = False
            if limiter is not None:
                try:
                    saturated = limiter.borrowed_tokens >= limiter.total_tokens
                except Exception:
                    pass
            busy_since = (busy_since or now) if saturated else None
            reason = (f"event loop unresponsive for {stall:.0f}s" if stall > STALL_S else
                      f"all {limiter.total_tokens} worker threads busy for {now - busy_since:.0f}s"
                      if busy_since and now - busy_since > STALL_S else None)
            if reason and not dumped:
                log.error("FREEZE DETECTED: %s - dumping every thread's stack below", reason)
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                dumped = True
            elif not reason:
                dumped = False

    threading.Thread(target=run, name="bt-watchdog", daemon=True).start()
