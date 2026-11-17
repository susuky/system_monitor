"""Opt-in GPU telemetry for exercising layouts without GPU hardware."""

from math import sin
import time


class DemoGPUs:
    def __init__(self, count: int) -> None:
        self.count = count
        self._started = time.monotonic()

    def sample(self) -> list[dict]:
        elapsed = time.monotonic() - self._started
        rows = []
        for index in range(self.count):
            utilization = round(50 + 45 * sin(elapsed / 7 + index * 1.7), 1)
            total = (8, 16, 24, 32)[index % 4] * 1024 ** 3
            fraction = 0.5 + 0.4 * sin(elapsed / 13 + index * 0.9)
            rows.append({
                "index": index, "name": f"Simulated GPU {index}",
                "uuid": f"SIMULATED-GPU-{index}", "simulated": True,
                "utilization": utilization, "memory_used": int(total * fraction),
                "memory_total": total, "temperature": round(35 + utilization * 0.4, 1),
                "power_watts": round(30 + utilization * 2, 1), "power_limit_watts": 250,
                "fan_percent": round(15 + utilization * 0.6, 1),
                "processes": [], "processes_supported": False,
            })
        return rows
