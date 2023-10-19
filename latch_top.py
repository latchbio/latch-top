import sys
import time
from typing import List, Union
import re

errors: List[str] = []

try:
    import psutil
except Exception as e:
    errors.append(f"{e}\nMissing required module psutil")

try:
    from tabulate import tabulate
except Exception as e:
    errors.append(f"{e}\nMissing required module tabulate")

if "linux" not in sys.platform:
    errors.append("Unsupported platform!")

if errors:
    print("ERROR!")
    print("\n".join(errors))
    raise SystemExit(1)


def si_unit(num: Union[int, float]) -> str:
    num = float(num)
    for unit in ("B", "k", "M", "G", "T", "P", "E", "Z"):
        if abs(num) < 1000:
            return f"{num:3.1f}{unit}"
        num /= 1000
    return f"{num:.1f}{unit}"


class ProcessStats:
    def __init__(self) -> None:
        self.memory_limit_bytes: int = 0
        self.used_memory_bytes: int = 0
        self.buff_cache_bytes: int = 0
        self.previous_total_sys_time: float = 0.0
        self.previous_total_user_time: float = 0.0
        self.total_sys_time: float = 0.0
        self.total_user_time: float = 0.0
        self.timestamp: float = 0
        self.previous_timestamp: float = 0

        try:
            with open("/sys/fs/cgroup/memory/memory.stat", "r") as file:
                memory_stat_content = file.read()

            hierarchical_memory_limit_match = re.search(
                r"hierarchical_memory_limit (\d+)", memory_stat_content
            )
            total_cache_match = re.search(r"total_cache (\d+)", memory_stat_content)

            if hierarchical_memory_limit_match:
                self.memory_limit_bytes = int(hierarchical_memory_limit_match.group(1))

            if total_cache_match:
                self.buff_cache_bytes = int(total_cache_match.group(1))

        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}")
            raise SystemExit(1)

        self.update()
        time.sleep(0.5)
        self.update()

    def update(self) -> None:
        self.previous_timestamp = self.timestamp
        self.timestamp = time.time()
        self.used_memory_bytes = 0
        self.previous_total_sys_time = self.total_sys_time
        self.previous_total_user_time = self.total_user_time
        self.total_sys_time = 0.0
        self.total_user_time = 0.0

        for process in psutil.process_iter():
            mem_info = process.memory_full_info()
            cpu_times = process.cpu_times()
            self.used_memory_bytes += mem_info.rss
            self.total_sys_time += cpu_times.system
            self.total_user_time += cpu_times.user

    def __str__(self) -> str:
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"Latch Top - {current_time}")

        total_memory_usage = self.used_memory_bytes + self.buff_cache_bytes
        memory_utilzation = (
            f"{si_unit(total_memory_usage)}/{si_unit(self.memory_limit_bytes)}"
        )
        memory_utilization_percentage = (
            total_memory_usage / self.memory_limit_bytes * 100
        )
        cpu_utilization_percentage = (
            self.total_sys_time
            - self.previous_total_sys_time
            + self.total_user_time
            - self.previous_total_user_time
        ) / (self.timestamp - self.previous_timestamp)

        res = [
            [
                "Memory",
                f"{memory_utilzation} ({memory_utilization_percentage:.1f}%)",
            ],
            [
                "CPU",
                f"{cpu_utilization_percentage:.1f}%",
            ],
        ]
        headers = ["Resource", "Utilization"]
        return tabulate(res, headers=headers, tablefmt="fancy_grid", stralign="center")


if __name__ == "__main__":
    process_stats = ProcessStats()
    print(process_stats)
