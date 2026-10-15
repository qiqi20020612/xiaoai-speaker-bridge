#!/usr/bin/env python3
"""Convert the text-free monotonic phase trace to inspectable per-PID durations."""

import argparse
import csv
import math
import re
import statistics
import sys
from collections import defaultdict

PATTERN = re.compile(r"pid=(\d+) phase=([a-z_]+) uptime=(\d+(?:\.\d+)?)(?: cycle=([\d.]+))?\n?")
WAKE_SPANS = (
    ("wake", "recording_launch"),
    ("recording_launch", "recording_first_output"),
    ("recording_launch", "recording_first_pcm"),
    ("wake", "recording_first_pcm"),
)


def read_points(stream):
    for line in stream:
        if match := PATTERN.fullmatch(line):
            pid, phase, at, cycle = match.groups()
            yield pid, phase, float(at), cycle


def intervals(points):
    previous = {}
    for pid, phase, now, cycle in points:
        key = ("cycle", cycle) if cycle else ("pid", pid)
        if key in previous:
            before, started, before_pid = previous[key]
            if now >= started:
                yield pid, before, phase, now - started, cycle or "", before_pid
        previous[key] = phase, now, pid


def distribution(values):
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max": ordered[-1],
    }


def summarize(points):
    points = list(points)
    samples = defaultdict(list)
    for _, before, after, elapsed, *_ in intervals(points):
        samples[before, after].append(elapsed)
    cycles = defaultdict(dict)
    for _, phase, at, cycle in points:
        if cycle:
            cycles[cycle].setdefault(phase, at)
    for before, after in WAKE_SPANS:
        # Recompute across complete cycles, including cycles with intervening
        # detailed phases. Never bridge two wakes of a persistent legacy PID.
        values = [
            phases[after] - phases[before]
            for phases in cycles.values()
            if before in phases and after in phases and phases[after] >= phases[before]
        ]
        samples.pop((before, after), None)
        if values:
            samples[before, after] = values
    return {pair: distribution(values) for pair, values in samples.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace")
    parser.add_argument("--summary", action="store_true", help="Cycle totals and nearest-rank P95")
    args = parser.parse_args()
    writer = csv.writer(sys.stdout)
    with open(args.trace) as stream:
        points = list(read_points(stream))
    if args.summary:
        writer.writerow(
            ["from_phase", "to_phase", "samples", "median_seconds", "p95_seconds", "max_seconds"]
        )
        for pair, stats in sorted(summarize(points).items()):
            writer.writerow(
                [*pair, stats["samples"], *[f"{stats[k]:.3f}" for k in ("median", "p95", "max")]]
            )
    else:
        writer.writerow(["pid", "from_phase", "to_phase", "elapsed_seconds", "cycle", "from_pid"])
        for pid, before, after, elapsed, cycle, before_pid in intervals(points):
            writer.writerow([pid, before, after, f"{elapsed:.3f}", cycle, before_pid])


if __name__ == "__main__":
    main()
