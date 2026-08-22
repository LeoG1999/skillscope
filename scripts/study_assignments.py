#!/usr/bin/env python3
"""Generate the four-sequence counterbalanced study task sheet."""

import argparse
import csv
import shlex
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SEQUENCES = (
    (("workspace", "travel-rebooking"), ("chat", "expense-review")),
    (("chat", "travel-rebooking"), ("workspace", "expense-review")),
    (("workspace", "expense-review"), ("chat", "travel-rebooking")),
    (("chat", "expense-review"), ("workspace", "travel-rebooking")),
)


def assignments(participants, start_port):
    rows = []
    for index, participant in enumerate(participants):
        sequence = SEQUENCES[index % len(SEQUENCES)]
        for period, (condition, scenario) in enumerate(sequence, 1):
            rows.append({
                "participant": participant,
                "sequence": chr(ord("A") + index % len(SEQUENCES)),
                "period": str(period),
                "condition": condition,
                "scenario": scenario,
                "port": str(start_port + index * 2 + period - 1),
            })
    return rows


def command(row, data_dir):
    values = [sys.executable, str(ROOT / "scripts" / "run_study_task.py"),
              "--participant", row["participant"], "--period", row["period"],
              "--condition", row["condition"], "--scenario", row["scenario"],
              "--port", row["port"], "--data-dir", str(data_dir)]
    return " ".join(shlex.quote(value) for value in values)


def main():
    parser = argparse.ArgumentParser(description="生成正式研究任务分配表")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--participants", nargs="+", help="参与者编号列表")
    group.add_argument("--count", type=int, help="生成 P001… 的数量")
    parser.add_argument("--start-port", type=int, default=8800)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "study-data")
    parser.add_argument("--output", type=Path, help="另存为 CSV；默认写到 stdout")
    args = parser.parse_args()
    participants = args.participants or ["P%03d" % value
                                         for value in range(1, args.count + 1)]
    if not participants or len(set(participants)) != len(participants):
        parser.error("参与者编号不能为空或重复")
    rows = assignments(participants, args.start_port)
    for row in rows:
        row["launch_command"] = command(row, args.data_dir.expanduser().resolve())
    fieldnames = ("participant", "sequence", "period", "condition", "scenario",
                  "port", "launch_command")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader();writer.writerows(rows)
        print("已写入 %d 个任务：%s" % (len(rows), args.output))
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader();writer.writerows(rows)


if __name__ == "__main__":
    main()
