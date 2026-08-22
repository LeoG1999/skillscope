#!/usr/bin/env python3
"""Exercise a complete scenario repair loop against a running server.

This mutates only the server's in-memory study state and publishes a test
version. Use a disposable server port for acceptance.

    python3 scripts/e2e_acceptance.py http://127.0.0.1:8776 travel-rebooking
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scenario_runtime as runtime


BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8776"
SCENARIO = sys.argv[2] if len(sys.argv) > 2 else "travel-rebooking"


def post(path, body):
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise AssertionError("%s returned HTTP %d: %s" %
                             (path, error.code, detail[:1000])) from error


def stream(path, body):
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        return [json.loads(line.decode()) for line in response if line.strip()]


def run(snapshot_id, draft=False):
    messages = stream("/api/run", {"snapshot": snapshot_id, "k": 3,
                                   "variant": {"draft": True} if draft else {}})
    rows = [message["run"] for message in messages if message.get("type") == "run"]
    assert len(rows) == 3
    assert all(not row.get("error") for row in rows)
    assert all(row["execution"]["facts_source"] == "tool-trace-and-world-state" for row in rows)
    return rows[0]


def main():
    pack = runtime.get_pack(SCENARIO)
    loaded = post("/api/scenario/load", {"id": pack["id"], "study": {
        "formal": True, "session": "e2e", "participant": "acceptance",
        "period": "1", "condition": "workspace",
    }})
    entry = loaded["snapshot"]
    baseline = {entry["case_id"]: run(entry["id"])}

    issue = runtime.analyze_issue(pack, entry, baseline[entry["case_id"]])
    assert issue and issue.get("suggestions"), "faulty incident did not expose the packaged issue"
    suggestion = issue["suggestions"][0]
    change = post("/api/situation", {
        "snapshot": entry["id"],
        "commitment": suggestion["commitment"],
        "criterion": suggestion["criterion"],
        "disposition": "change", "label": suggestion["criterion"]["label"],
        "case_source": "incident", "case_context": entry["task"],
    })
    assert change["situation"]["pre_reveal"]

    suggestions = post("/api/contrast", {
        "snapshot": entry["id"], "commitment": change["situation"]["commitment"]})
    assert suggestions["source"] == "intent-conditioned-frozen-case-bank"
    assert len(suggestions["situations"]) == 3
    assert suggestions.get("intent", {}).get("summary")
    assert suggestions.get("plan_hash")
    assert {row["suggest"] for row in suggestions["situations"]} == {"preserve", "unresolved"}

    snapshots = {entry["case_id"]: entry}
    for suggestion in suggestions["situations"]:
        built = post("/api/case", {
            "snapshot": entry["id"], "case_id": suggestion["case_id"],
            "description": suggestion["text"], "relation": suggestion["why"],
        })["snapshot"]
        snapshots[built["case_id"]] = built
        baseline[built["case_id"]] = run(built["id"])
        review_prompt = suggestion.get("review_prompt") or {}
        if suggestion["suggest"] == "preserve":
            body = {
                "commitment": review_prompt["baseline_commitment"],
                "criterion": review_prompt["baseline_criterion"],
                "disposition": "preserve",
                "label": review_prompt["baseline_criterion"]["label"],
                "sealed": suggestion.get("exposure") == "generator-withheld",
            }
        else:
            body = {
                "commitment": review_prompt["unresolved_commitment"],
                "criterion": None, "disposition": "unresolved", "label": "", "sealed": False,
            }
        body.update({"snapshot": built["id"], "case_source": built["source"],
                     "case_context": built["task"]})
        saved = post("/api/situation", body)["situation"]
        assert saved["generator_exposure"] == (
            "withheld" if suggestion.get("exposure") == "generator-withheld" else "visible")

    preview = post("/api/repair_preview", {})["preview"]
    probes = preview["evidence"]
    assert preview["instruction"]
    assert preview["hash"]
    assert preview["assessment"]["status"] in ("related", "uncertain", "not_confirmed")
    assert "临时移除" not in preview.get("question", "")
    assert "最小反转" not in preview.get("question", "")
    assert probes
    for probe in probes:
        assert probe["evidence_role"] == "source_location"
        assert probe["baseline_artifact_hash"] == loaded["skill"]["content_hash"]
        assert len(probe["intervention_run_ids"]) == 3
        assert probe["held_constant"]["world_hash"] == entry["world_hash"]
        assert probe.get("baseline_outcome")
        assert probe.get("intervention_outcome")
    selected_probe_ids = [row["id"] for row in probes]

    manifest = post("/api/manifest", {"source_evidence_ids": selected_probe_ids})
    assert manifest["scope_version"] == 5
    assert len(manifest["visible"]["expectations"]) == 3
    assert len(manifest["withheld"]["expectations"]) == 1
    assert [row["id"] for row in manifest["visible"]["source_evidence"]] == selected_probe_ids
    assert manifest["repair_preview_id"] == preview["id"]
    assert manifest["repair_preview_hash"] == preview["hash"]
    drafted = post("/api/draft", {"manifest_id": manifest["id"], "feedback": []})["candidate"]
    assert drafted["content_hash"] != loaded["skill"]["content_hash"], \
        "candidate generator returned an unchanged skill"
    assert drafted["input_manifest"]["hash"] == manifest["hash"]
    assert drafted["input_manifest"]["scope_plan_hash"] == suggestions["plan_hash"]
    assert drafted["input_manifest"]["repair_preview_hash"] == preview["hash"]
    assert any(row["candidate_author"] == "withheld" for row in drafted["case_exposure"])

    # Use the researcher-only reference implementation to make release checks deterministic.
    edited = post("/api/edit", {
        "instructions": pack["skill"]["reference_instructions"]})["candidate"]
    assert edited["author"] == "owner"
    assert all(row["candidate_author"] != "withheld" for row in edited["case_exposure"])

    after = {case_id: run(snapshot["id"], draft=True)
             for case_id, snapshot in snapshots.items()}
    for case_id, row in after.items():
        case = runtime.get_case(pack, case_id)
        if case["role"] in ("incident", "generator-withheld", "owner-visible-preserve"):
            assert runtime.evaluate_oracle(pack, case_id, row["facts"])[
                "all_required_passed"], "reference patch failed %s" % case_id
    unresolved_id = next(case_id for case_id in snapshots
                         if runtime.get_case(pack, case_id)["role"] ==
                         "owner-visible-boundary")
    needs_judgment = (baseline[unresolved_id]["facts"] != after[unresolved_id]["facts"])

    situations = post("/api/export", {})["situations"]
    outcome = [{"situation_id": row["id"], "case_id": row.get("case_id"),
                "expectation": row["commitment"], "disposition": row["disposition"],
                "conflict": False,
                "needs_judgment": row["disposition"] == "unresolved" and needs_judgment,
                "insufficient": False} for row in situations]
    published = post("/api/publish", {
        "reason": "自动验收：确认目标和保留项通过，并记录未解决边界的人工豁免。",
        "outcome": outcome,
    })
    review = published["review"]
    assert review["record_hash"]
    assert review["evidence"]["manifest_hash"] == manifest["hash"]
    linked_runs = {run_id for case in review["evidence"]["cases"]
                   for run_id in case["baseline_runs"] + case["candidate_runs"]}
    assert not linked_runs.intersection({run_id for probe in probes
                                         for run_id in probe["intervention_run_ids"]})
    assert len(published["skill"]["regression_cases"]) == 3
    assert published["skill"]["version"] == 2
    exported = post("/api/export", {})
    assert exported["format"] == "skillscope/2"
    assert any(row.get("_oracle") for row in exported["runs"])

    print("PASS: %s incident → versioned scope → exposure-aware manifest → draft → matched tool runs → release record" % SCENARIO)
    print("skill=v%d scope=v%d regression_cases=%d review_hash=%s" % (
        published["skill"]["version"], review["scope_version"],
        len(published["skill"]["regression_cases"]), review["record_hash"][:12]))
    print("unresolved_changed=%s (Needs judgment only when the paired result changes)" %
          needs_judgment)


if __name__ == "__main__":
    main()
