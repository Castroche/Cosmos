from __future__ import annotations

from collections import defaultdict
from threading import Lock
from time import monotonic


class Metrics:
    max_series = 10_000

    def __init__(self):
        self.started_at = monotonic()
        self._lock = Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def increment(self, name: str, value: float = 1, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            if key not in self._counters and len(self._counters) >= self.max_series:
                return
            self._counters[key] += value

    def gauge(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            if key not in self._gauges and len(self._gauges) >= self.max_series:
                return
            self._gauges[key] = value

    def render_prometheus(self) -> str:
        with self._lock:
            samples = list(self._counters.items()) + list(self._gauges.items())
        lines = [f"cosmos_uptime_seconds {monotonic() - self.started_at:.3f}"]
        for (name, labels), value in sorted(samples):
            label_text = ""
            if labels:
                encoded = ",".join(f'{key}="{self._escape(item)}"' for key, item in labels)
                label_text = f"{{{encoded}}}"
            lines.append(f"cosmos_{name}{label_text} {value:g}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
