import sys
import time
from typing import List, OrderedDict, Union
from collections import OrderedDict
import re

errors: List[str] = []

try:
    import psutil
except Exception as e:
    errors.append(f"{e}\nMissing required module psutil")

if "linux" not in sys.platform:
    errors.append(f"Unsupported platform: {sys.platform}. Only Linux is supported!")

if len(errors) > 0:
    print("ERROR!")
    print("\n".join(errors))
    raise SystemExit(1)


def si_unit(num: Union[int, float]) -> str:
    num = float(num)
    for unit in ("B", "k", "M", "G", "T", "P", "E", "Z"):
        if abs(num) < 1000:
            break
        num /= 1000
    return f"{num:.1f}{unit}"


def parse_memory_stat_content(
    pattern: str, value_name: str, memory_stat_content: str
) -> int:
    match = re.search(pattern, memory_stat_content)
    if match:
        value = match.group(1)
        try:
            return int(value)
        except ValueError:
            print(
                f"Error: unable to convert value of {value_name}, {value} to type int"
            )
            raise SystemExit(1)
    else:
        print(f'Error: "{value_name}" not found in "/sys/fs/cgroup/memory/memory.stat"')
        raise SystemExit(1)


class ProcessInfo:
    def __init__(
        self,
        pid: int,
        user: str,
        res: int,
        sys_time: float,
        user_time: float,
        command: str,
    ) -> None:
        self.pid: int = pid
        self.user: str = user
        self.res: int = res
        self.previous_sys_time: float = sys_time  # TODO(rt): might temporarily see incorrect values for individual processes
        self.previous_user_time: float = user_time
        self.sys_time: float = sys_time
        self.user_time: float = user_time
        self.command: str = command
        self.percent_cpu: float = 0.0
        self.percent_mem: float = 0.0

    def update_stats(
        self,
        new_sys_time: float,
        new_user_time: float,
        new_res: int,
        memory_limit_bytes: int,
        total_cpu_time: float,
    ) -> None:
        self.previous_sys_time = self.sys_time
        self.previous_user_time = self.user_time
        self.sys_time = new_sys_time
        self.user_time = new_user_time
        self.res = new_res
        self.percent_cpu = (
            (
                self.sys_time
                - self.previous_sys_time
                + self.user_time
                - self.previous_user_time
            )
            / total_cpu_time
        ) * 100
        self.percent_mem = (self.res / memory_limit_bytes) * 100

    def __str__(self) -> str:
        cpu_slice_minutes, remainder = divmod(self.user_time + self.sys_time, 60)
        cpu_slice_seconds, cpu_slice_hundreths = divmod(remainder, 1)
        cpu_slice_hundreths = int(cpu_slice_hundreths * 100)
        cpu_slice_time = f"{int(cpu_slice_minutes)}:{int(cpu_slice_seconds):02d}.{cpu_slice_hundreths:02d}"  # format: M:SS.hh
        body = f"{self.pid:5} {self.user:8} {si_unit(self.res):>6} {self.percent_mem:>5.1f} {self.percent_cpu:>5.1f} {cpu_slice_time:>10} {self.command:<20}"
        return body


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
        self.processes: OrderedDict[int, ProcessInfo] = OrderedDict()

        memory_stat_content = ""
        try:
            with open("/sys/fs/cgroup/memory/memory.stat", "r") as file:
                memory_stat_content = file.read()

        except FileNotFoundError:
            print('Error: file "/sys/fs/cgroup/memory/memory.stat" not found')
            raise SystemExit(1)

        self.memory_limit_bytes = parse_memory_stat_content(
            pattern=r"hierarchical_memory_limit (\d+)",
            value_name="hierarchical_memory_limit",
            memory_stat_content=memory_stat_content,
        )
        self.buff_cache_bytes = parse_memory_stat_content(
            pattern=r"total_cache (\d+)",
            value_name="total_cache",
            memory_stat_content=memory_stat_content,
        )

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
            if not self.processes.get(process.pid):
                self.processes[process.pid] = ProcessInfo(
                    pid=process.pid,
                    user=process.username()
                    if len(process.username()) <= 8
                    else process.username()[0:7] + "+",
                    res=mem_info.rss,
                    sys_time=cpu_times.system,
                    user_time=cpu_times.user,
                    command=process.name(),
                )
            else:
                self.processes[process.pid].update_stats(
                    cpu_times.system,
                    cpu_times.user,
                    mem_info.rss,
                    self.memory_limit_bytes,
                    self.timestamp - self.previous_timestamp,
                )

    def print(self) -> None:
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")

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

        reverse_terminal_colors = "\033[7m"
        default_terminal_colors = "\033[0m"
        column_names = f"{reverse_terminal_colors}{'PID':>5} {'USER':<8} {'MEM':>6} {'%MEM':>5} {'%CPU':>5} {'RUNTIME':>10} {'COMMAND':<20}{default_terminal_colors}"
        print(f"\nDate: {current_time}")
        print(f"MEM: {memory_utilzation} ({memory_utilization_percentage:.1f}%)")
        print(f"CPU: {cpu_utilization_percentage:.1f}% \n")
        print(column_names)
        for process in sorted(
            self.processes.values(),
            key=lambda x: (x.percent_mem, x.percent_cpu),
            reverse=True,
        ):
            print(process)


if __name__ == "__main__":
    process_stats = ProcessStats()
    process_stats.print()
