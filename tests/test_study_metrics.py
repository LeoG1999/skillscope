import unittest

from scripts import study_metrics


class StudyMetricsTests(unittest.TestCase):
    def test_terminal_decision_classification_preserves_reasonable_nonrelease(self):
        warning = {"action": "defer", "reason": "仍有边界未决",
                   "outcome": [{"needs_judgment": True}]}
        self.assertEqual(("justified_nonrelease", True, False),
                         study_metrics.decision_classification(warning))

        clear = {"action": "defer", "reason": "我还想继续观察", "outcome": []}
        self.assertEqual(("nonrelease_requires_rationale_coding", None, True),
                         study_metrics.decision_classification(clear))
        clear["rationale_coding"] = "unsupported"
        self.assertEqual(("unnecessary_nonrelease", False, False),
                         study_metrics.decision_classification(clear))

        unsafe = {"action": "publish", "reason": "仍然发布",
                  "outcome": [{"conflict": True}]}
        self.assertEqual(("unsafe_release", False, False),
                         study_metrics.decision_classification(unsafe))

    def test_extracts_behavioral_and_questionnaire_metrics(self):
        document = {
            "skill": {"id": "k1", "scenario_id": "travel-rebooking",
                      "work_order": {"id": "travel-review", "task_hash": "taskhash"},
                      "study_context": {"participant": "P07", "period": "2",
                                        "formal": True, "started_at": 15,
                                        "task_id": "travel-review", "task_hash": "taskhash"}},
            "snapshots": [{"id": "s1", "case_role": "incident", "recorded": 10}],
            "runs": [{"id": "r1", "steps": [{}, {}],
                      "_oracle": {"all_required_passed": True}}],
            "situations": [{"id": "t1", "pre_reveal": True}],
            "probes": [{"id": "p1"}],
            "events": [{"at": 12, "name": name, "semantic": True}
                       for name in ("task_started", "intent_committed", "scope_committed",
                                    "candidate_revealed", "comparison_viewed",
                                    "decision_submitted", "task_completed")],
            "chat": [],
            "reviews": [{
                "id": "v1", "action": "publish", "condition": "workspace", "at": 40,
                "situations": [{"id": "t1", "disposition": "change"}],
                "outcome": [{"situation_id": "t1", "disposition": "change",
                             "conflict": False}],
                "evidence": {"manifest_hash": "manifest",
                             "cases": [{"situation_id": "t1", "snapshot": "s1",
                                        "baseline_runs": ["r1"],
                                        "candidate_runs": ["r1"]}]},
            }],
            "questionnaires": [{
                "id": "q1", "review_id": "v1", "scenario_id": "travel-rebooking",
                "condition": "workspace", "status": "completed",
                "artifact": {"hash": "artifact"},
                "measurement": {"prediction_accuracy": 1.0, "mean_confidence": 86,
                                "mean_confidence_correct": 86,
                                "high_confidence_error_rate": 0,
                                "prediction_items": [{"correct": True}] * 6,
                                "oracle_pass_rate": 1.0},
                "research_holdout": {"oracle_pass_rate": .67, "modal_share": .67},
                "ratings": {"understand_change": 6},
                "workload": {"mental_demand": 30, "effort": 50},
            }],
        }

        row = study_metrics.task_row(document, "sample.json")

        self.assertEqual("workspace", row["condition"])
        self.assertEqual(1.0, row["scope_alignment"])
        self.assertEqual(1.0, row["scope_classification_accuracy"])
        self.assertEqual("supported_release", row["decision_category"])
        self.assertTrue(row["evidence_supported_decision"])
        self.assertTrue(row["evidence_trace_complete"])
        self.assertEqual(1, row["pre_reveal_scope_count"])
        self.assertFalse(row["unsafe_release"])
        self.assertEqual(1.0, row["incident_oracle_pass_rate"])
        self.assertEqual(.67, row["research_holdout_oracle_pass_rate"])
        self.assertEqual(2, row["tool_call_count"])
        self.assertTrue(row["semantic_path_complete"])
        self.assertEqual(7, row["semantic_event_count"])
        self.assertEqual(6, row["prediction_item_count"])
        self.assertEqual(86, row["prediction_mean_confidence"])
        self.assertEqual(25, row["task_duration_seconds"])
        self.assertEqual("P07", row["participant_id"])
        self.assertEqual("2", row["period"])
        self.assertTrue(row["formal_session"])
        self.assertEqual("travel-review", row["task_id"])
        self.assertEqual("taskhash", row["task_hash"])


if __name__ == "__main__":
    unittest.main()
