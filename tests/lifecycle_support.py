"""Deterministic process/executor fixtures, with no app or model imports."""
from collections import deque
from concurrent.futures import Future
import subprocess

class _ControlledExecutor:
    """Represent queued transitions without nondeterministic thread timing."""

    def __init__(self, **_kwargs):
        self.jobs = deque()
        self.closed = False
        self.shutdown_arguments = None

    def submit(self, function, *args):
        if self.closed:
            raise RuntimeError("cannot schedule new futures after shutdown")
        future = Future()
        self.jobs.append((function, args, future))
        return future

    def run_next(self):
        function, args, future = self.jobs.popleft()
        future.set_running_or_notify_cancel()
        try:
            result = function(*args)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        future.set_result(result)

    def drain(self):
        while self.jobs:
            self.run_next()

    def shutdown(self, *, wait, cancel_futures):
        self.closed = True
        self.shutdown_arguments = (wait, cancel_futures)


class FakeProcess:
    def __init__(self, trace, pid):
        self.trace, self.pid, self.returncode = trace, pid, None
        self.wait_timeout = False

    def poll(self):
        return self.returncode

    def wait(self, *, timeout):
        self.trace.append(("process.wait", self.pid, timeout))
        if self.wait_timeout:
            self.wait_timeout = False
            raise subprocess.TimeoutExpired("owned-fake", timeout)
        self.returncode = 0
        return 0

    def kill(self):
        self.trace.append(("process.kill", self.pid))
        self.returncode = -1

