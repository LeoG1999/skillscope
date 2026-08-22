import copy
import unittest

import scenario_runtime as runtime


class ScenarioRuntimeTests(unittest.TestCase):
    def test_packs_have_distinct_exposure_roles(self):
        self.assertEqual({"travel-rebooking", "expense-review"}, set(runtime.PACKS))
        for pack in runtime.PACKS.values():
            roles = {case["role"] for case in pack["cases"]}
            self.assertIn("incident", roles)
            self.assertIn("generator-withheld", roles)
            self.assertIn("owner-visible-preserve", roles)
            self.assertIn("owner-visible-boundary", roles)
            self.assertIn("prediction-holdout", roles)
            self.assertIn("research-holdout", roles)

    def test_each_formal_domain_has_two_by_three_unseen_predictions(self):
        for pack in runtime.PACKS.values():
            cases = [case for case in pack["cases"]
                     if case["role"] == "prediction-holdout"]
            self.assertEqual(2, len(cases), pack["id"])
            self.assertEqual(6, sum(len(case["study_measure"]["prediction_questions"])
                                    for case in cases), pack["id"])
            ids = [question["id"] for case in cases
                   for question in case["study_measure"]["prediction_questions"]]
            self.assertEqual(len(ids), len(set(ids)), pack["id"])
            neighbours = runtime.neighbouring_cases(pack, pack["entry_case"])
            self.assertTrue(neighbours)
            self.assertEqual(3, len(neighbours))
            holdout_ids = {case["id"] for case in pack["cases"]
                           if runtime.participant_hidden(case)}
            self.assertTrue(holdout_ids.isdisjoint({row["case_id"] for row in neighbours}))
            prediction = next(case for case in pack["cases"]
                              if case["role"] == "prediction-holdout")
            self.assertTrue(prediction["study_measure"]["prediction_questions"])
            self.assertGreaterEqual(len(prediction["study_measure"]["brief"]), 3)

    def test_every_scored_case_declares_faulty_and_reference_calibration(self):
        for pack in runtime.PACKS.values():
            for case in pack["cases"]:
                if not case.get("oracle"):
                    continue
                calibration = case.get("calibration_expectation") or {}
                self.assertEqual({"faulty", "reference"}, set(calibration))
                self.assertTrue(set(calibration.values()) <= {"pass", "fail"})

    def test_each_pack_has_a_versioned_participant_work_order(self):
        public = {row["id"]: row for row in runtime.public_scenarios()}
        for pack_id, pack in runtime.PACKS.items():
            order = runtime.public_work_order(pack)
            self.assertEqual(order, public[pack_id]["work_order"])
            self.assertEqual(pack_id, order["scenario_id"])
            self.assertEqual(12, len(order["task_hash"]))
            self.assertGreater(order["time_minutes"], 0)
            for key in ("role", "context", "objective", "completion",
                        "environment_note"):
                self.assertTrue(order[key].strip())
            serialized = str(order).lower()
            self.assertNotIn("oracle", serialized)
            self.assertNotIn("reference", serialized)
            self.assertNotIn("change", serialized)
            self.assertNotIn("preserve", serialized)
            self.assertNotIn("unresolved", serialized)

    def test_formal_source_preview_is_single_and_versioned_in_each_pack(self):
        for pack in runtime.PACKS.values():
            previews = pack.get("source_preview_defaults") or []
            self.assertEqual(1, len(previews))
            preview = previews[0]
            self.assertEqual(pack["entry_case"], preview["case_id"])
            self.assertTrue(preview["question"].strip())
            self.assertTrue(preview["inverted_text"].strip())
            source = next(row for row in pack["skill"]["faulty_instructions"]
                          if row["n"] == preview["instruction"])
            self.assertNotEqual(source["text"], preview["inverted_text"])

    def test_incident_issue_is_derived_from_execution_not_snapshot(self):
        travel = runtime.get_pack("travel-rebooking")
        snapshot = runtime.case_snapshot(travel, travel["entry_case"])
        self.assertNotIn("review_prompt", snapshot)
        self.assertNotIn("oracle", snapshot)
        issue = runtime.analyze_issue(travel, snapshot, {"facts": {
            "selected_flight": "UA1123",
            "arrival_time": "2026-03-13T14:20:00-05:00",
            "price": 180,
            "on_time_for_commitment": False,
        }})
        self.assertEqual("execution-derived", issue["source"])
        self.assertIn("UA1123", issue["issue_detail"])
        self.assertIn("5 小时 20 分钟", issue["issue_detail"])
        self.assertEqual("on_time_for_commitment",
                         issue["suggestions"][0]["criterion"]["spec"]["key"])
        self.assertIsNone(runtime.analyze_issue(travel, snapshot, {"facts": {
            "selected_flight": "AA455",
            "arrival_time": "2026-03-12T21:20:00-05:00",
            "price": 640,
            "on_time_for_commitment": True,
        }}))

        neighbours = runtime.neighbouring_cases(travel, travel["entry_case"])
        preserve = next(row for row in neighbours if row["suggest"] == "preserve")
        boundary = next(row for row in neighbours if row["suggest"] == "unresolved")
        self.assertEqual("selected_flight",
                         preserve["review_prompt"]["baseline_criterion"]["spec"]["key"])
        self.assertTrue(boundary["review_prompt"]["unresolved_commitment"])

    def test_expense_issue_uses_actual_decision_facts(self):
        pack = runtime.get_pack("expense-review")
        snapshot = runtime.case_snapshot(pack, pack["entry_case"])
        issue = runtime.analyze_issue(pack, snapshot, {"facts": {
            "receipt_id": "R-8891", "decision": "approve",
            "ledger_entry_created": True, "manager_approval_requested": False,
        }})
        self.assertIn("直接通过并已入账", issue["issue_detail"])
        self.assertEqual("manager_approval_requested",
                         issue["suggestions"][0]["criterion"]["spec"]["key"])
        self.assertIsNone(runtime.analyze_issue(pack, snapshot, {"facts": {
            "receipt_id": "R-8891", "decision": "escalate",
            "ledger_entry_created": False, "manager_approval_requested": True,
        }}))

    def test_each_snapshot_is_an_isolated_world(self):
        pack = runtime.get_pack("travel-rebooking")
        first = runtime.case_snapshot(pack, pack["entry_case"])
        second = runtime.case_snapshot(pack, pack["entry_case"])
        self.assertEqual(first["world_hash"], second["world_hash"])
        result = runtime.dispatch(pack, first["world"], "booking_rebook",
                                  {"flight_id": "UA1123"})
        self.assertTrue(result["ok"])
        self.assertTrue(first["world"]["state"]["booking_attempts"])
        self.assertNotIn("booking_attempts", second["world"]["state"])

    def test_travel_oracle_uses_tool_state_not_model_text(self):
        pack = runtime.get_pack("travel-rebooking")
        snapshot = runtime.case_snapshot(pack, "travel-incident")
        world = copy.deepcopy(snapshot["world"])
        trace = []
        for api_name, args in (
                ("calendar_commitments", {"from": "2026-03-12T12:30:00-07:00",
                                          "to": "2026-03-13T23:59:59-05:00"}),
                ("flights_search", {"origin": "SFO", "destination": "ORD",
                                    "after": "2026-03-12T12:30:00-07:00"}),
                ("traveler_request_confirmation", {"action": "改签 AA455", "amount": 640}),
                ("booking_rebook", {"flight_id": "AA455", "confirmation_id": "CONF-001"})):
            result = runtime.dispatch(pack, world, api_name, args)
            trace.append(runtime.tool_step(pack, api_name, args, result,
                                           pack["skill"]["reference_instructions"]))
        facts = runtime.derive_facts(pack, world, trace)
        oracle = runtime.evaluate_oracle(pack, "travel-incident", facts)
        self.assertEqual("AA455", facts["selected_flight"])
        self.assertTrue(facts["confirmation_before_booking"])
        self.assertTrue(oracle["all_required_passed"])

        faulty_world = copy.deepcopy(snapshot["world"])
        result = runtime.dispatch(pack, faulty_world, "booking_rebook",
                                  {"flight_id": "UA1123"})
        faulty_trace = [runtime.tool_step(pack, "booking_rebook", {"flight_id": "UA1123"},
                                          result, pack["skill"]["faulty_instructions"])]
        faulty_facts = runtime.derive_facts(pack, faulty_world, faulty_trace)
        faulty_oracle = runtime.evaluate_oracle(pack, "travel-incident", faulty_facts)
        self.assertFalse(faulty_oracle["all_required_passed"])

    def test_expense_oracle_detects_low_amount_policy_bypass(self):
        pack = runtime.get_pack("expense-review")
        snapshot = runtime.case_snapshot(pack, "expense-incident")
        world = copy.deepcopy(snapshot["world"])
        direct = runtime.dispatch(pack, world, "review_record_decision", {
            "receipt_id": "R-8891", "decision": "approve", "reason": "低于日常额度"})
        post = runtime.dispatch(pack, world, "ledger_post", {"receipt_id": "R-8891"})
        trace = [runtime.tool_step(pack, "review_record_decision", {}, direct, []),
                 runtime.tool_step(pack, "ledger_post", {}, post, [])]
        facts = runtime.derive_facts(pack, world, trace)
        self.assertFalse(runtime.evaluate_oracle(
            pack, "expense-incident", facts)["all_required_passed"])

        reviewed = copy.deepcopy(snapshot["world"])
        approval = runtime.dispatch(pack, reviewed, "manager_request_approval", {
            "receipt_id": "R-8891", "reason": "受限品类"})
        decision = runtime.dispatch(pack, reviewed, "review_record_decision", {
            "receipt_id": "R-8891", "decision": "escalate", "reason": "等待主管批准"})
        trace = [runtime.tool_step(pack, "manager_request_approval", {}, approval, []),
                 runtime.tool_step(pack, "review_record_decision", {}, decision, [])]
        facts = runtime.derive_facts(pack, reviewed, trace)
        self.assertTrue(runtime.evaluate_oracle(
            pack, "expense-incident", facts)["all_required_passed"])

    def test_typed_arguments_are_validated(self):
        pack = runtime.get_pack("travel-rebooking")
        snapshot = runtime.case_snapshot(pack, "travel-incident")
        result = runtime.dispatch(pack, snapshot["world"], "flights_search", {
            "origin": "SFO", "destination": "ORD"})
        self.assertFalse(result["ok"])
        self.assertIn("after", result["error"])


if __name__ == "__main__":
    unittest.main()
