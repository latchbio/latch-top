import time
from typing import Union, List, Tuple, Dict, Optional, Set
import re
from pathlib import Path
import traceback
from dataclasses import dataclass
import shlex
import os
from argparse import ArgumentParser
import atexit

argp = ArgumentParser(
    "latch-top", description="Show true resource usage in a latch pod"
)
argp.add_argument(
    "-w",
    "--watch",
    nargs="?",
    const=1.5,
    default=None,
    type=float,
    help="Show live update of resource usage",
)
argp.add_argument(
    "-2", "--base-2", action="store_true", help="Use base 2 units instead of SI units"
)
argp.add_argument(
    "-s", "--sort-by", default="cpu", choices=["cpu", "mem", "pid"], help="Sort field"
)
argp.add_argument(
    "-H", "--hierarchical", action="store_true", help="Show processes as a tree"
)
argp.add_argument(
    "-f", "--full-commands", action="store_true", help="Show full process command lines"
)
argp.add_argument(
    "--cputime-sample-time",
    default=0.5,
    type=float,
    help="Minimum duration of the CPU usage sample",
)
args = argp.parse_args()

# >>> Utils

clock_ticks_per_second = os.sysconf("SC_CLK_TCK")
clock_tick_duration = 1.0 / clock_ticks_per_second

memory_limit = int(Path("/sys/fs/cgroup/memory.max").read_text())

cpu_limit = int(Path("/root/.latch/latch-pod-cpu-quota").read_text())

si_units = ["B", "k", "M", "G", "T", "P", "E", "Z"]
base2_units = ["B", "ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]


def si_unit(num: Union[int, float]) -> Tuple[str, str]:
    units = si_units if not args.base_2 else base2_units
    unit_base = 1000 if not args.base_2 else 1024

    num = float(num)
    for unit in units:
        if abs(num) < unit_base:
            return f"{num:.1f}", unit
        num /= unit_base

    return f"{num:.3f}", "B"


smaps_entry_re = re.compile(
    r"""
    (?P<address_low>[0-9a-z]+)
    -
    (?P<address_high>[0-9a-z]+)
    [ ]+
    (?P<perms>[rwxsp\-]+)
    [ ]+
    (?P<offset>[0-9a-z]+)
    [ ]+
    (?P<device_major>[0-9a-z]+)
    :
    (?P<device_minor>[0-9a-z]+)
    [ ]+
    (?P<inode>[0-9]+)
    (
        [ ]+
        (?P<pathname>[^\n]+)
    )?

    [ ]*
    \n

    (?P<properties>
        (\w+:[^\n]+\n)*
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

property_line_re = re.compile(
    r"""
    (?P<name>\S+)
    :
    \s+
    (?P<value>\S+)
    ( kB)?
    \s*
    """,
    re.VERBOSE | re.IGNORECASE,
)


@dataclass
class CpuTimeSample:
    real_time: float
    cpu_time: float


@dataclass
class Datum:
    pid: int
    comm: str
    mem: int
    cmd_line: str
    cpu_time: float = 0


ansi_re = re.compile(r"\x1b\[[^m]*m")

alt_screen_on = "\x1b[?1049h\x1b[?25l"
alt_screen_off = "\x1b[?25h\x1b[?1049l"

reset_cursor = "\x1b[H"
clear_rest_line = "\x1b[K"
clear_rest = "\x1b[J"

bold = "\x1b[1m"
dim = "\x1b[2m"
italic = "\x1b[3m"
uline = "\x1b[4m"
fg_green = "\x1b[32m"
fg_bgreen = "\x1b[1;32m"
fg_blue = "\x1b[34m"
fg_bblue = "\x1b[1;34m"
reset = "\x1b[0m"

field_align = ["r", "r", "l", "r", "l", "l", "l"]

proc_p = Path("/proc")

last_sample_time: Optional[float] = None
cpu_samples: Dict[int, List[CpuTimeSample]] = {}


def run():
    pids: Set[int] = set()
    for cur_proc in proc_p.iterdir():
        if not cur_proc.name.isdigit():
            continue

        pids.add(int(cur_proc.name))

    # >>> Get CPU stats

    children_by_ppid: Dict[int, List[int]] = {}

    def take_cpu_sample():
        global last_sample_time

        now = time.monotonic()

        if last_sample_time is not None and now - last_sample_time < 0.5:
            time.sleep(args.cputime_sample_time - (now - last_sample_time))
            now = time.monotonic()

        last_sample_time = now

        for pid in pids:
            try:
                p = proc_p / str(pid) / "stat"
                sample_time = time.monotonic()

                stat_pid, raw_stats = p.read_text().split(" ", maxsplit=1)

                # the process name (stat[2] might have spaces in it
                stats = [stat_pid, *raw_stats.rsplit(" ", maxsplit=50)]

                ppid = int(stats[3])
                children_by_ppid.setdefault(ppid, []).append(pid)

                utime = int(stats[13]) * clock_tick_duration
                stime = int(stats[14]) * clock_tick_duration
                cpu_time = utime + stime

                samples = cpu_samples.setdefault(pid, [])
                if len(samples) == 2:
                    samples = cpu_samples[pid] = samples[1:]

                samples.append(CpuTimeSample(sample_time, cpu_time))
            except FileNotFoundError:
                continue
            except Exception:
                traceback.print_exc()

    if last_sample_time is None:
        take_cpu_sample()

    # >>> Get RAM stats

    data_by_pid: Dict[int, Datum] = {}
    for pid in pids:
        cur_proc = proc_p / str(pid)
        try:
            smap_p = cur_proc / "smaps"

            total_mem = 0
            for smap in smaps_entry_re.finditer(smap_p.read_text()):
                for prop in smap.group("properties").split("\n"):
                    m = property_line_re.match(prop)
                    if m is None:
                        continue

                    if m.group("name").lower() != "pss":
                        continue

                    mem = int(m.group("value")) * 1024
                    total_mem += mem
                    break

            cmd_line = ""
            if args.full_commands:
                cmd_line = shlex.join(
                    (cur_proc / "cmdline").read_text().split("\0")[:-1]
                )

            data_by_pid[pid] = Datum(
                pid=pid,
                comm=(cur_proc / "comm").read_text().strip(),
                mem=total_mem,
                cmd_line=cmd_line,
            )
        except FileNotFoundError:
            continue
        except Exception:
            traceback.print_exc()

    # >>> Process

    take_cpu_sample()

    for pid, samples in cpu_samples.items():
        # if pid not in pids:
        #     del cpu_samples[pid]
        #     continue

        if pid not in data_by_pid:
            continue

        if len(samples) < 2:
            del data_by_pid[pid]
            continue

        start, end = samples
        data_by_pid[pid].cpu_time = (end.cpu_time - start.cpu_time) / (
            end.real_time - start.real_time
        )

    data = list(data_by_pid.values())

    if args.sort_by == "pid":
        data.sort(key=lambda x: x.pid)
    elif args.sort_by == "mem":
        data.sort(key=lambda x: -x.mem)
    elif args.sort_by == "cpu":
        # a lot of processes have 0 cpu usage, sort them by RAM
        data.sort(key=lambda x: -x.mem)
        data.sort(key=lambda x: -x.cpu_time)

    # >>> Render
    table: List[List[str]] = [
        [
            f"{uline}PID{reset}",
            f"{uline}RAM{reset}",
            f"{uline}%{reset}",
            f"{uline}CPU{reset}",
            f"{uline}Quota %{reset}",
            f"{uline}Name{reset}",
            f"{uline}Command{reset}" if args.full_commands else "",
        ]
    ]

    mem_total = 0
    cpu_time_total = 0

    def render_datum(x: Datum, *, indent: str = ""):
        nonlocal mem_total, cpu_time_total

        mem_total += x.mem
        mem, mem_unit = si_unit(x.mem)

        cpu_time_total += x.cpu_time

        cpu_time_pct = 100 * (x.cpu_time / cpu_limit)
        mem_pct = 100 * x.mem / memory_limit
        table.append(
            [
                str(x.pid),
                f"{fg_bgreen}{mem}{reset}{fg_green}{mem_unit}{reset}",
                f"{fg_green}[{fg_bgreen}{mem_pct:>5.1f}{reset}{fg_green}%]{reset}",
                f"{fg_bblue}{x.cpu_time:.1f}{reset}",
                f"{fg_blue}[{fg_bblue}{cpu_time_pct:>5.1f}{reset}{fg_blue}%]{reset}",
                indent + x.comm,
                f"{dim}{x.cmd_line}{reset}",
            ]
        )

    seen: Set[int] = set()

    def render_hierarchy(x: Datum, *, indent: str = ""):
        if x.pid in seen:
            return
        seen.add(x.pid)

        render_datum(x, indent=indent)

        # display children in the sort-by order
        child_pids = set(children_by_ppid.get(x.pid, []))
        for datum in data:
            if datum.pid not in child_pids:
                continue

            render_hierarchy(datum, indent=indent + "  ")

    if args.hierarchical:
        render_hierarchy(data_by_pid[1])
    else:
        for x in data:
            render_datum(x)

    mem, mem_unit = si_unit(mem_total)

    cpu_time_pct = 100 * (cpu_time_total / cpu_limit)
    mem_pct = 100 * mem_total / memory_limit
    table.append([])
    table.append(
        [
            f"{uline}Total{reset}",
            f"{fg_bgreen}{mem}{reset}{fg_green}{mem_unit}{reset}",
            f"{fg_green}[{fg_bgreen}{mem_pct:>5.1f}{reset}{fg_green}%]{reset}",
            f"{fg_bblue}{cpu_time_total:.1f}{reset}",
            f"{fg_blue}[{fg_bblue}{cpu_time_pct:>5.1f}{reset}{fg_blue}%]{reset}",
            "",
            "",
        ]
    )

    mem, mem_unit = si_unit(memory_limit)
    table.append(
        [
            f"{dim}Quota{reset}",
            f"{fg_bgreen}{mem}{reset}{fg_green}{mem_unit}{reset}",
            "",
            f"{fg_bblue}{cpu_limit:.1f}{reset}",
            "",
            "",
            "",
        ]
    )

    field_len: List[int] = []
    for row in table:
        for idx, field in enumerate(row):
            while len(field_len) <= idx:
                field_len.append(0)

            field_len[idx] = max(field_len[idx], len(ansi_re.sub("", field)))

    if args.watch is not None:
        print(reset_cursor)

    for row in table:
        line = ""
        for idx, field in enumerate(row):
            flen = field_len[idx]

            align = field_align[idx]

            l = len(ansi_re.sub("", field))
            pad = " " * (flen - l)

            if align == "r":
                line += f"{pad}{field}"
            else:
                if idx == len(row) - 1:
                    # do not break line wrapping with extraneous whitespace
                    pad = ""

                line += f"{field}{pad}"

            line += " "
        print(line + clear_rest_line)

    if args.watch is not None:
        print(clear_rest)


# >>> Main loop
if args.watch is not None:

    def disable_alt_screen():
        print(alt_screen_off)

    atexit.register(disable_alt_screen)


def main():
    try:
        if args.watch is not None:
            print(alt_screen_on + reset_cursor + clear_rest)

        while True:
            run()

            if args.watch is None:
                break

            time.sleep(args.watch)
    except KeyboardInterrupt:
        ...


if __name__ == "__main__":
    main()
