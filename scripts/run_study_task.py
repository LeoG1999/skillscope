#!/usr/bin/env python3
"""Start one isolated formal SkillScope task with automatic recovery.

Example:
    python3 scripts/run_study_task.py \
      --participant P001 --period 1 --condition workspace \
      --scenario travel-rebooking --port 8801 --data-dir study-data
"""

import argparse
import os
import re
import sys
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scenario_runtime as runtime  # noqa: E402


def archive_component(value, fallback):
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-.")
    return (value or fallback)[:80]


def archive_name(participant, period, condition, scenario):
    order = runtime.public_work_order(runtime.get_pack(scenario))
    fields = [archive_component(participant, "participant"),
              "p" + archive_component(period, "unknown"),
              archive_component(condition, "condition"),
              archive_component(scenario, "scenario"),
              archive_component(order["task_hash"], "task")[:16]]
    return "__".join(fields) + ".json"


def task_url(port, participant, period, condition, scenario):
    path = "/" if condition == "workspace" else "/chat"
    query = urllib.parse.urlencode({
        "study": "1", "scenario": scenario,
        "participant": participant, "period": period,
    })
    return "http://127.0.0.1:%d%s?%s" % (port, path, query)


def main():
    parser = argparse.ArgumentParser(description="启动一个隔离的正式研究任务")
    parser.add_argument("--participant", required=True)
    parser.add_argument("--period", required=True)
    parser.add_argument("--condition", choices=("workspace", "chat"), required=True)
    parser.add_argument("--scenario", choices=tuple(row["id"] for row in
                                                     runtime.public_scenarios()),
                        required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--no-resume", action="store_true",
                        help="若已有检查点则拒绝启动，而不是恢复")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port 必须在 1024 到 65535 之间")
    if not args.participant.strip() or not str(args.period).strip():
        parser.error("participant 和 period 不能为空")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("请先设置 DEEPSEEK_API_KEY")

    participant = args.participant.strip()
    period = str(args.period).strip()
    data_dir = args.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = data_dir / archive_name(
        participant, period, args.condition, args.scenario)
    if checkpoint.exists() and args.no_resume:
        parser.error("该任务已有记录：%s；移除 --no-resume 可安全恢复" % checkpoint)

    env = os.environ.copy()
    env["PORT"] = str(args.port)
    env["SKILLSCOPE_DATA_DIR"] = str(data_dir)
    env["SKILLSCOPE_REQUIRE_ARCHIVE"] = "1"
    env["SKILLSCOPE_PARTICIPANT"] = participant
    env["SKILLSCOPE_PERIOD"] = period
    env["SKILLSCOPE_CONDITION"] = args.condition
    env["SKILLSCOPE_SCENARIO"] = args.scenario
    if checkpoint.exists():
        env["SKILLSCOPE_RESUME_FILE"] = str(checkpoint)
        state = "恢复已有检查点"
    else:
        env.pop("SKILLSCOPE_RESUME_FILE", None)
        state = "创建新任务"
    url = task_url(args.port, participant, period,
                   args.condition, args.scenario)
    print("%s：%s" % (state, checkpoint), flush=True)
    print("参与者链接：%s" % url, flush=True)
    os.execve(sys.executable, [sys.executable, str(ROOT / "server.py")], env)


if __name__ == "__main__":
    main()
