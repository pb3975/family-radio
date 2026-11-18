"""Cached operating-system clock synchronization, outside the USB loop."""
import subprocess
import threading
import time


def system_synchronized():
    try:
        result = subprocess.run(['timedatectl', 'show', '--property=NTPSynchronized', '--value'],
                                capture_output=True, text=True, timeout=2, check=True)
        return result.stdout.strip() == 'yes'
    except (OSError, subprocess.SubprocessError):
        return False


class ClockSync:
    def __init__(self, probe=system_synchronized, now=time.monotonic):
        self.probe, self.now = probe, now
        self.checked_at = None
        self.synchronized = False
        self.stop = threading.Event()
        self.worker = None

    def refresh(self):
        self.synchronized = self.probe()
        self.checked_at = self.now()

    def ready(self):
        return self.synchronized and self.checked_at is not None and self.now()-self.checked_at < 60

    def _run(self):
        while not self.stop.is_set():
            self.refresh()
            self.stop.wait(30)

    def __enter__(self):
        self.worker = threading.Thread(target=self._run, name='radio-clock', daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.worker.join(timeout=3)
