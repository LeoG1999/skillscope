#!/usr/bin/env python3
"""Black-box acceptance check for a running SkillScope server.

Usage:
    python3 scripts/acceptance.py http://127.0.0.1:8775 travel-rebooking 3
"""

import json
import sys
import urllib.request
from collections import Counter


BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8775"
SCENARIO = sys.argv[2] if len(sys.argv) > 2 else "travel-rebooking"
RUNS = int(sys.argv[3]) if len(sys.argv) > 3 else 3


def post(path, body):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(request, timeout=600)


def main():
    loaded = json.load(post("/api/scenario/load", {"id": SCENARIO}))
    snapshot = loaded["snapshot"]
    messages = [json.loads(line.decode()) for line in
                post("/api/run", {"snapshot": snapshot["id"], "k": RUNS})
                if line.strip()]
    public_runs = [row["run"] for row in messages if row.get("type") == "run"]
    done = next(row for row in messages if row.get("type") == "done")
    assert len(public_runs) == RUNS, "run count mismatch"
    for run in public_runs:
        assert not run.get("error"), run.get("error")
        assert run.get("steps"), "agent made no tool calls"
        assert run.get("execution", {}).get("runtime") == "skillscope/tool-world/1"
        assert run["execution"].get("facts_source") == "tool-trace-and-world-state"
        assert "_oracle" not in run, "hidden oracle leaked to participant response"
        assert all(step.get("tool") and isinstance(step.get("args"), dict)
                   for step in run["steps"])

    exported = json.load(post("/api/export", {}))
    stored = [row for row in exported["runs"] if row["id"] in
              {run["id"] for run in public_runs}]
    assert len(stored) == RUNS
    assert all("_oracle" in row for row in stored), "research oracle not retained"
    signatures = Counter(json.dumps(run["facts"], sort_keys=True, ensure_ascii=False)
                         for run in public_runs)
    oracle_passes = sum(1 for run in stored if run["_oracle"]["all_required_passed"])

    print("PASS: real tool loop, isolated runtime metadata, hidden oracle, and export provenance")
    print("scenario=%s case=%s runs=%d modal_share=%.2f oracle_all_pass=%d/%d" % (
        SCENARIO, snapshot["case_id"], RUNS,
        max(signatures.values()) / float(RUNS), oracle_passes, RUNS))
    for signature, count in signatures.most_common():
        facts = json.loads(signature)
        print("  %dx %s" % (count, json.dumps(facts, ensure_ascii=False, sort_keys=True)))
    print("summary_share=%.2f pack_hash=%s world_hash=%s" % (
        done["summary"]["share"], loaded["skill"]["scenario_pack_hash"][:12],
        snapshot["world_hash"][:12]))


if __name__ == "__main__":
    main()
