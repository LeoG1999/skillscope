#!/usr/bin/env python3
"""Measure faulty/reference behavior without exposing holdouts through HTTP.

Requires DEEPSEEK_API_KEY. A quick smoke run uses N=1; before the formal study,
run N=20 and archive the output with model and pack hashes.

    python3 scripts/calibrate_scenarios.py all 5
"""

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scenario_runtime as runtime
import server


TARGET = sys.argv[1] if len(sys.argv) > 1 else "all"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 1


def evaluate(pack, case, variant):
    instructions = pack["skill"][variant + "_instructions"]
    snapshot = runtime.case_snapshot(pack, case["id"])
    rows = [server._scenario_exec_once(instructions, snapshot) for _ in range(N)]
    errors = [row for row in rows if row.get("error")]
    valid = [row for row in rows if not row.get("error")]
    passes = sum(1 for row in valid if row["_oracle"]["all_required_passed"])
    signatures = collections.Counter(
        json.dumps(row["facts"], ensure_ascii=False, sort_keys=True) for row in valid)
    return {
        "valid": len(valid), "errors": len(errors), "passes": passes,
        "pass_rate": passes / float(len(valid)) if valid else 0,
        "modal_share": max(signatures.values()) / float(len(valid)) if valid else 0,
    }


def main():
    if not server.API_KEY:
        raise SystemExit("DEEPSEEK_API_KEY is required")
    ids = list(runtime.PACKS) if TARGET == "all" else [TARGET]
    failed = False
    for pack_id in ids:
        pack = runtime.get_pack(pack_id)
        print("%s pack=%s model=%s review_temperature=%s agent_temperature=%s N=%d" % (
            pack_id, pack["pack_hash"][:12], server.MODEL, server.REVIEW_TEMPERATURE,
            server.AGENT_TEMPERATURE, N))
        for case in pack["cases"]:
            if not case.get("oracle"):
                continue
            variants = ("faulty", "reference")
            for variant in variants:
                result = evaluate(pack, case, variant)
                print("  %-28s %-9s pass=%d/%d rate=%.2f modal=%.2f errors=%d" % (
                    case["id"], variant, result["passes"], result["valid"],
                    result["pass_rate"], result["modal_share"], result["errors"]))
                expected = (case.get("calibration_expectation") or {}).get(variant)
                if expected not in ("pass", "fail"):
                    raise RuntimeError(
                        "missing calibration_expectation.%s for %s" %
                        (variant, case["id"]))
                if expected == "fail":
                    good = result["pass_rate"] <= .20 and result["modal_share"] >= .80
                else:
                    good = result["pass_rate"] >= .80 and result["modal_share"] >= .80
                failed = failed or not good
                print("    expected=%s gate=%s" %
                      (expected, "PASS" if good else "FAIL"))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
