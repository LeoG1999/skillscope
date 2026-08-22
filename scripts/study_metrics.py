#!/usr/bin/env python3
"""Extract one analysis-ready row from each SkillScope task export.

The script never calls a model. It derives outcomes from frozen review records,
tool-world oracle results, questionnaire scoring, and interaction logs.

    python3 scripts/study_metrics.py exports/*.json
    python3 scripts/study_metrics.py --csv exports/*.json > task-level.csv
"""

import csv
import json
import sys
from pathlib import Path


def mean(values):
    values = [value for value in values if isinstance(value, (int, float))]
    return sum(values) / float(len(values)) if values else None


def oracle_pass_rate(run_ids, runs):
    selected = [runs[run_id] for run_id in run_ids if run_id in runs]
    valid = [row for row in selected if not row.get("error") and row.get("_oracle")]
    return mean([1 if row["_oracle"].get("all_required_passed") else 0 for row in valid])


def decision_classification(review):
    """Classify the terminal action without treating every non-release as failure.

    A clear-case defer remains uncoded until a researcher reviews the written
    rationale. Set ``rationale_coding`` to ``supported`` or ``unsupported`` in
    the review record to resolve it without changing the original response.
    """
    outcomes = review.get("outcome") or []
    action = review.get("action")
    blockers = any(any(item.get(key) for key in
                       ("conflict", "insufficient", "execution_failed"))
                   for item in outcomes)
    unresolved = any(item.get("needs_judgment") for item in outcomes)
    reason = (review.get("reason") or "").strip()
    coding = review.get("rationale_coding")
    if action == "publish":
        if blockers:
            return "unsafe_release", False, False
        if unresolved and not reason:
            return "unsafe_release", False, False
        return "supported_release", True, False
    if action == "defer":
        if blockers or unresolved or coding == "supported":
            return "justified_nonrelease", True, False
        if coding == "unsupported":
            return "unnecessary_nonrelease", False, False
        return "nonrelease_requires_rationale_coding", None, True
    return None, None, False


def task_row(document, source=""):
    skill = document.get("skill") or {}
    reviews = document.get("reviews") or []
    questionnaires = document.get("questionnaires") or []
    completed = [row for row in questionnaires if row.get("status") == "completed"]
    questionnaire = completed[-1] if completed else (questionnaires[-1] if questionnaires else {})
    review = next((row for row in reversed(reviews)
                   if row.get("id") == questionnaire.get("review_id")), None)
    if review is None:
        review = next((row for row in reversed(reviews)
                       if row.get("action") in ("publish", "defer")), {})

    outcomes = review.get("outcome") or []
    aligned_rows = [row for row in outcomes
                    if row.get("disposition") in ("change", "preserve")]
    aligned = [not any(row.get(key) for key in
                       ("conflict", "insufficient", "execution_failed"))
               for row in aligned_rows]
    action = review.get("action")
    unsafe_release = bool(action == "publish" and any(not value for value in aligned))
    decision_category, evidence_supported, rationale_needs_coding = \
        decision_classification(review)

    snapshots = {row.get("id"): row for row in document.get("snapshots") or []}
    runs = {row.get("id"): row for row in document.get("runs") or []}
    role_rates = {}
    evidence_cases = ((review.get("evidence") or {}).get("cases") or [])
    evidence_by_situation = {row.get("situation_id"): row for row in evidence_cases}
    for evidence in evidence_cases:
        role = (snapshots.get(evidence.get("snapshot")) or {}).get("case_role")
        if not role:
            continue
        value = oracle_pass_rate(evidence.get("candidate_runs") or [], runs)
        if value is not None:
            role_rates.setdefault(role, []).append(value)

    measurement = questionnaire.get("measurement") or {}
    blind = questionnaire.get("research_holdout") or {}
    events = document.get("events") or []
    semantic_events = [row for row in events
                       if row.get("semantic") or row.get("name") in (
                           "task_started", "intent_committed", "scope_committed",
                           "candidate_revealed", "comparison_viewed",
                           "decision_submitted", "task_completed")]
    semantic_names = {row.get("name") for row in semantic_events}
    times = [row.get("recorded") for row in document.get("snapshots") or []]
    times.extend(row.get("at") for row in events)
    times = [value for value in times if isinstance(value, (int, float))]
    decision_at = review.get("at")
    duration = (decision_at - min(times)) if times and isinstance(decision_at, (int, float)) else None
    ratings = questionnaire.get("ratings") or {}
    workload = questionnaire.get("workload") or {}
    study = questionnaire.get("study_context") or skill.get("study_context") or {}
    assigned_start = study.get("started_at")
    if isinstance(assigned_start, (int, float)) and isinstance(decision_at, (int, float)):
        duration = decision_at - assigned_start

    situations = document.get("situations") or []
    situation_by_id = {item.get("id"): item for item in situations}
    reviewed_scope = review.get("situations") or []
    if not reviewed_scope:
        reviewed_scope = [item for item in situations if not item.get("superseded_at")]
    expected_dispositions = {
        "incident": "change",
        "owner-visible-preserve": "preserve",
        "generator-withheld": "preserve",
    }
    scope_classification = []
    for item in reviewed_scope:
        evidence = evidence_by_situation.get(item.get("id")) or {}
        role = (snapshots.get(evidence.get("snapshot")) or {}).get("case_role")
        if role in expected_dispositions:
            scope_classification.append(item.get("disposition") == expected_dispositions[role])
    preview_id = (review.get("evidence") or {}).get("repair_preview_id")
    preview = next((item for item in skill.get("repair_previews") or []
                    if item.get("id") == preview_id), {})
    source_probe_ids = set((review.get("evidence") or {}).get("source_interventions") or [])
    source_probes = [item for item in document.get("probes") or []
                     if item.get("id") in source_probe_ids]
    traceable = bool((review.get("evidence") or {}).get("manifest_hash") and evidence_cases and
                     all(item.get("baseline_runs") and item.get("candidate_runs")
                         for item in evidence_cases))

    row = {
        "source": source,
        "skill_id": skill.get("id"),
        "scenario_id": questionnaire.get("scenario_id") or skill.get("scenario_id"),
        "condition": questionnaire.get("condition") or review.get("condition"),
        "participant_id": study.get("participant"),
        "period": study.get("period"),
        "formal_session": study.get("formal"),
        "task_id": study.get("task_id") or (skill.get("work_order") or {}).get("id"),
        "task_hash": study.get("task_hash") or (skill.get("work_order") or {}).get("task_hash"),
        "review_action": action,
        "artifact_hash": ((questionnaire.get("artifact") or {}).get("hash") or
                          review.get("candidate_content_hash")),
        "task_duration_seconds": duration,
        "scope_item_count": len(reviewed_scope),
        "scope_change_count": sum(1 for item in reviewed_scope
                                  if item.get("disposition") == "change"),
        "scope_preserve_count": sum(1 for item in reviewed_scope
                                    if item.get("disposition") == "preserve"),
        "scope_unresolved_count": sum(1 for item in reviewed_scope
                                      if item.get("disposition") == "unresolved"),
        "scope_excluded_count": sum(1 for item in reviewed_scope
                                    if item.get("disposition") == "excluded"),
        "pre_reveal_scope_count": sum(1 for item in reviewed_scope
                                      if (situation_by_id.get(item.get("id")) or {}).get(
                                          "pre_reveal")),
        "scope_classification_accuracy": mean(
            [1 if value else 0 for value in scope_classification]),
        "scope_alignment": mean([1 if value else 0 for value in aligned]),
        "candidate_behavior_alignment": mean([1 if value else 0 for value in aligned]),
        "decision_category": decision_category,
        "evidence_supported_decision": evidence_supported,
        "decision_rationale_requires_coding": rationale_needs_coding,
        "unsafe_release": decision_category == "unsafe_release",
        "evidence_trace_complete": traceable,
        "release_warning_count": sum(1 for item in outcomes if any(
            item.get(key) for key in ("conflict", "needs_judgment", "insufficient"))),
        "unresolved_changed_count": sum(1 for item in outcomes
                                        if item.get("disposition") == "unresolved" and
                                        item.get("needs_judgment")),
        "prediction_accuracy": measurement.get("prediction_accuracy"),
        "prediction_item_count": len(measurement.get("prediction_items") or []),
        "prediction_mean_confidence": measurement.get("mean_confidence"),
        "prediction_mean_confidence_correct": measurement.get("mean_confidence_correct"),
        "prediction_mean_confidence_incorrect": measurement.get("mean_confidence_incorrect"),
        "prediction_high_confidence_error_rate": measurement.get(
            "high_confidence_error_rate"),
        "prediction_holdout_oracle_pass_rate": measurement.get("oracle_pass_rate"),
        "research_holdout_oracle_pass_rate": blind.get("oracle_pass_rate"),
        "research_holdout_modal_share": blind.get("modal_share"),
        "run_count": len(document.get("runs") or []),
        "tool_call_count": sum((row.get("execution") or {}).get("tool_calls",
                                                                 len(row.get("steps") or []))
                               for row in document.get("runs") or []),
        "probe_count": len(document.get("probes") or []),
        "frozen_source_probe_count": len(source_probes),
        "frozen_source_probe_run_count": sum(len(item.get("intervention_run_ids") or [])
                                             for item in source_probes),
        "source_location_cue_used": bool(preview and
                                         (preview.get("used_at") or preview.get("confirmed_at"))),
        "candidate_revision_count": len(skill.get("candidate_rounds") or []),
        "scope_revision_count": max(0, int(skill.get("review_round") or 1) - 1),
        "semantic_event_count": len(semantic_events),
        "semantic_path_complete": all(name in semantic_names for name in (
            "task_started", "intent_committed", "scope_committed",
            "candidate_revealed", "comparison_viewed", "decision_submitted",
            "task_completed")),
    }
    for role, values in role_rates.items():
        row[role.replace("-", "_") + "_oracle_pass_rate"] = mean(values)
    for key, value in ratings.items():
        row["rating_" + key] = value
    for key, value in workload.items():
        row["workload_" + key] = value
    return row


def main(argv):
    csv_mode = "--csv" in argv
    paths = [Path(value) for value in argv[1:] if value != "--csv"]
    if not paths:
        raise SystemExit("usage: study_metrics.py [--csv] export.json [...]")
    rows = []
    for path in paths:
        with path.open(encoding="utf-8") as stream:
            rows.append(task_row(json.load(stream), str(path)))
    if not csv_mode:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    writer = csv.DictWriter(sys.stdout, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)


if __name__ == "__main__":
    main(sys.argv)
