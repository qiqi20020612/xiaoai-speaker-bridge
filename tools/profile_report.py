#!/usr/bin/env python3
"""Convert the text-free monotonic phase trace to inspectable per-PID durations."""

import argparse
import csv
import re
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace")
    args = parser.parse_args()
    previous = {}
    writer = csv.writer(sys.stdout)
    writer.writerow(["pid", "from_phase", "to_phase", "elapsed_seconds", "cycle", "from_pid"])
    with open(args.trace) as stream:
        for line in stream:
            match = re.fullmatch(
                r"pid=(\d+) phase=([a-z_]+) uptime=(\d+(?:\.\d+)?)(?: cycle=([\d.]+))?\n?",
                line,
            )
            if match is None:
                continue
            pid, phase, at, cycle = match.groups()
            now = float(at)
            key = ("cycle", cycle) if cycle else ("pid", pid)
            if key in previous:
                before, started, before_pid = previous[key]
                if now >= started:
                    writer.writerow(
                        [pid, before, phase, f"{now - started:.3f}", cycle or "", before_pid]
                    )
            previous[key] = phase, now, pid


if __name__ == "__main__":
    main()
