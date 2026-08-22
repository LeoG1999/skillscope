#!/usr/bin/env python3
"""Calibrate the actual AI drafting path on a canonical, researcher-side scope.

This complements ``calibrate_scenarios.py``: that script validates faulty and
reference artifacts, while this script samples the candidate generator that
participants actually use. Holdout identities and scores are printed only in
this local researcher tool and never pass through the product API.

    DEEPSEEK_API_KEY=... python3 scripts/calibrate_candidates.py all 5 1
"""

import collections
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scenario_runtime as runtime
import server


TARGET = sys.argv[1] if len(sys.argv) > 1 else "all"
SAMPLES = int(sys.argv[2]) if len(sys.argv) > 2 else 1
RUNS_PER_CASE = int(sys.argv[3]) if len(sys.argv) > 3 else 1


def canonical_expectations(pack):
    entry = runtime.get_case(pack, pack["entry_case"])
    snapshot = runtime.case_snapshot(pack, entry["id"])
    baseline = server._scenario_exec_once(pack["skill"]["faulty_instructions"], snapshot)
    issue = runtime.analyze_issue(pack, snapshot, baseline)
    suggestions = (issue or {}).get("suggestions") or []
    if not suggestions:
        raise RuntimeError("canonical incident did not produce an execution-derived repair choice")
    expectations = [{
        "id": "canonical-incident",
        "expectation": suggestions[0]["commitment"],
        "disposition": "change",
        "task": entry["task"],
    }]
    for neighbour in entry.get("neighbours", []):
        if neighbour.get("exposure") == "generator-withheld":
            continue
        case = runtime.get_case(pack, neighbour["case_id"])
        prompt = case.get("review_prompt") or {}
        if neighbour.get("suggest") == "preserve":
            text = prompt.get("baseline_commitment")
        else:
            text = prompt.get("unresolved_commitment")
        if not text:
            raise RuntimeError("missing canonical review prompt for %s" % case["id"])
        expectations.append({
            "id": "canonical-" + case["id"],
            "expectation": text,
            "disposition": neighbour["suggest"],
            "task": case["task"],
        })
    return expectations


def canonical_source_evidence(pack):
    """Execute the same frozen formal M0 source preview used before drafting."""
    defaults = pack.get("source_preview_defaults") or []
    if len(defaults) != 1:
        raise RuntimeError("formal pack must freeze exactly one source preview")
    plan = defaults[0]
    number = plan["instruction"]
    inverted_text = (plan.get("inverted_text") or "").strip()
    if not inverted_text:
        raise RuntimeError("formal source preview is missing its frozen inversion")
    original = copy.deepcopy(pack["skill"]["faulty_instructions"])
    if not any(row.get("n") == number for row in original):
        raise RuntimeError("frozen source instruction does not exist")
    snapshot = runtime.case_snapshot(pack, plan.get("case_id") or pack["entry_case"])

    variants = {
        "delete": [row for row in original if row.get("n") != number],
        "invert": [{"n": row.get("n"),
                    "text": inverted_text if row.get("n") == number else row.get("text")}
                   for row in original],
    }
    baseline = [server._scenario_exec_once(original, snapshot) for _ in range(3)]
    if any(row.get("error") for row in baseline):
        raise RuntimeError("source-preview baseline execution failed")
    evidence = []
    for kind, instructions in variants.items():
        runs = [server._scenario_exec_once(instructions, snapshot) for _ in range(3)]
        if any(row.get("error") for row in runs):
            raise RuntimeError("source-preview %s execution failed" % kind)
        keys = server.discriminating(baseline, runs)
        changed, fields, weak = server.compare_fields(baseline, runs, keys)
        evidence.append({
            "id": "canonical-source-" + kind,
            "instruction": number, "probe": kind, "changed": changed,
            "confidence": "unstable" if weak else "ok",
            "note": ("temporarily delete instruction %d" % number if kind == "delete"
                     else "temporarily invert instruction %d: %s" %
                     (number, inverted_text)),
            "changed_fields": fields,
        })
    return evidence


def draft_once(pack, expectations, evidence):
    result = server.generate_candidate(
        pack["skill"]["faulty_instructions"], expectations, evidence)
    if not result or not isinstance(result.get("instructions"), list):
        raise RuntimeError("candidate draft failed: %s" % result)
    return result["instructions"]


def evaluate(pack):
    expectations = canonical_expectations(pack)
    source_evidence = canonical_source_evidence(pack)
    faulty_hash = runtime.canonical_hash(pack["skill"]["faulty_instructions"])
    artifacts = collections.Counter()
    artifact_diffs = []
    case_scores = {case["id"]: [0, 0, 0] for case in pack["cases"] if case.get("oracle")}
    unchanged = 0
    errors = []
    for sample in range(SAMPLES):
        try:
            instructions = draft_once(pack, expectations, source_evidence)
        except Exception as exc:  # noqa: BLE001
            errors.append("draft %d: %s" % (sample + 1, exc))
            continue
        artifact_hash = runtime.canonical_hash(instructions)
        artifacts[artifact_hash] += 1
        unchanged += artifact_hash == faulty_hash
        old_by_number = {row["n"]: row["text"]
                         for row in pack["skill"]["faulty_instructions"]}
        artifact_diffs.append({
            "hash": artifact_hash,
            "changed": [{"n": row["n"], "text": row["text"]}
                        for row in instructions
                        if old_by_number.get(row["n"]) != row["text"]],
        })
        for case in pack["cases"]:
            if not case.get("oracle"):
                continue
            snapshot = runtime.case_snapshot(pack, case["id"])
            for _ in range(RUNS_PER_CASE):
                row = server._scenario_exec_once(instructions, snapshot)
                score = case_scores[case["id"]]
                score[2] += 1
                if row.get("error"):
                    score[1] += 1
                elif (row.get("_oracle") or {}).get("all_required_passed"):
                    score[0] += 1
    generated = sum(artifacts.values())
    return {
        "expectations": expectations, "source_evidence": source_evidence,
        "generated": generated,
        "draft_errors": errors,
        "unchanged": unchanged,
        "artifact_modal_share": (max(artifacts.values()) / float(generated)) if generated else 0,
        "unique_artifacts": len(artifacts), "artifact_diffs": artifact_diffs,
        "case_scores": case_scores,
    }


def main():
    if not server.API_KEY:
        raise SystemExit("DEEPSEEK_API_KEY is required")
    if SAMPLES < 1 or RUNS_PER_CASE < 1:
        raise SystemExit("sample counts must be positive")
    ids = list(runtime.PACKS) if TARGET == "all" else [TARGET]
    failed = False
    for pack_id in ids:
        pack = runtime.get_pack(pack_id)
        result = evaluate(pack)
        print("%s pack=%s model=%s review_temperature=%s agent_temperature=%s drafts=%d runs_per_case=%d" % (
            pack_id, pack["pack_hash"][:12], server.MODEL, server.REVIEW_TEMPERATURE,
            server.AGENT_TEMPERATURE, SAMPLES, RUNS_PER_CASE))
        print("  artifacts generated=%d unique=%d modal=%.2f unchanged=%d errors=%d" % (
            result["generated"], result["unique_artifacts"],
            result["artifact_modal_share"], result["unchanged"],
            len(result["draft_errors"])))
        for artifact in result["artifact_diffs"]:
            changed = "; ".join("%s. %s" % (row["n"], row["text"])
                                for row in artifact["changed"])
            print("    artifact=%s changed=%s" %
                  (artifact["hash"][:12], changed or "none"))
        print("  source_preview probes=%d runs_per_intervention=3 fields=%s" % (
            len(result["source_evidence"]),
            ",".join(sorted({field for row in result["source_evidence"]
                             for field in row.get("changed_fields") or []})) or "none"))
        for case in pack["cases"]:
            if case["id"] not in result["case_scores"]:
                continue
            passed, errors, total = result["case_scores"][case["id"]]
            valid = total - errors
            rate = passed / float(valid) if valid else 0
            print("  %-34s role=%-22s pass=%d/%d rate=%.2f errors=%d" % (
                case["id"], case["role"], passed, valid, rate, errors))
            if case["role"] in ("incident", "owner-visible-preserve"):
                failed = failed or rate < .80
        failed = failed or bool(result["draft_errors"]) or bool(result["unchanged"])
        failed = failed or result["artifact_modal_share"] < .80
        for error in result["draft_errors"]:
            print("    ERROR %s" % error)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
