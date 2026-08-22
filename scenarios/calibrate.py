#!/usr/bin/env python3
"""场景校验：固定跑 N 次，报告各行为维度的占比与误差范围。

调参之前先用它量一次，调完再量一次。6 次执行的抽样误差约 ±0.2，
跟目标区间一样宽，用它调参等于在噪声里找信号。

    python3 calibrate.py <port> <skill.md> <data.json> <fixture.json> <task.txt> [N]
"""

import json
import math
import sys
import urllib.request
from collections import Counter

B = None


def post(path, body, stream=False, timeout=600):
    r = urllib.request.Request(B + path, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    f = urllib.request.urlopen(r, timeout=timeout)
    if not stream:
        return json.load(f)
    return [json.loads(l.decode().strip()) for l in f if l.decode().strip()]


def wilson(k, n, z=1.96):
    """占比的置信区间。n 小的时候正态近似不可用，Wilson 区间才靠谱。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def scalars(run):
    out = {}
    for k, v in (run.get("facts") or {}).items():
        if isinstance(v, bool) or isinstance(v, (int, float)):
            out[k] = str(v).lower()
        elif isinstance(v, str) and len(v) <= 40 and not v[:4].isdigit():
            out[k] = v.strip().lower()
    return out


def main():
    global B
    port, skill_f, data_f, fix_f, task_f = sys.argv[1:6]
    N = int(sys.argv[6]) if len(sys.argv) > 6 else 20
    B = "http://127.0.0.1:%s" % port

    post("/api/skill", {"text": open(skill_f, encoding="utf-8").read()}, timeout=180)
    post("/api/seed", json.load(open(data_f, encoding="utf-8")))
    if fix_f != "-":
        post("/api/fixture", json.load(open(fix_f, encoding="utf-8")))
    task = open(task_f, encoding="utf-8").read().strip()

    snap = post("/api/snapshot", {"task": task}, timeout=180)["snapshot"]
    print("快照 %s" % snap["id"])
    for t, v in snap["tools"].items():
        print("  %-24s %s" % (t, len(v) if isinstance(v, list) else "-"))
    if snap.get("missing"):
        print("  !! 缺数据源:", snap["missing"])

    runs, errs = [], 0
    done = 0
    while done < N:
        k = min(5, N - done)
        for m in post("/api/run", {"snapshot": snap["id"], "k": k}, stream=True, timeout=600):
            if m.get("type") == "run":
                r = m["run"]
                if r.get("error"):
                    errs += 1
                else:
                    runs.append(r)
        done += k
        print("  已完成 %d/%d" % (done, N), flush=True)

    print("\n有效执行 %d 次，失败 %d 次" % (len(runs), errs))
    if not runs:
        return

    keys = {}
    for r in runs:
        for k, v in scalars(r).items():
            keys.setdefault(k, []).append(v)
    print("\n各维度主导取值（只列取值有变化的维度）：")
    print("  %-30s %-20s %-8s %s" % ("维度", "主导取值", "占比", "95% 区间"))
    for k in sorted(keys, key=lambda x: -len(set(keys[x]))):
        vals = keys[k]
        if len(set(vals)) < 2 or len(vals) < len(runs) * 0.6:
            continue
        top, cnt = Counter(vals).most_common(1)[0]
        lo, hi = wilson(cnt, len(vals))
        print("  %-30s %-20s %.2f     [%.2f, %.2f]  %s" % (
            k, top[:20], cnt / len(vals), lo, hi, dict(Counter(vals))))

    const = [k for k in keys if len(set(keys[k])) == 1]
    if const:
        print("\n恒定维度（无法区分行为）：%s" % ", ".join(const[:8]))


if __name__ == "__main__":
    main()
