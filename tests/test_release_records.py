import copy
import http.client
import inspect
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import scenario_runtime as runtime
import server


class ReleaseRecordTests(unittest.TestCase):
    def setUp(self):
        self.original_state = server.STATE
        server.STATE = {"skills": {}, "active": None, "snapshots": {},
                        "runs": [], "situations": [], "probes": [],
                        "reviews": [], "events": [], "chat": [],
                        "questionnaires": [], "seq": 0}

    def tearDown(self):
        server.STATE = self.original_state

    def test_legacy_scenario_skill_rehydrates_only_the_public_work_order(self):
        pack = runtime.get_pack("travel-rebooking")
        skill = runtime.skill_record(pack)
        skill.pop("work_order", None)
        skill.pop("study_context", None)

        server.initialize_skill_record(skill)

        self.assertEqual("travel-rebooking-review-v1", skill["work_order"]["id"])
        self.assertEqual({}, skill["study_context"])
        self.assertNotIn("cases", skill["work_order"])
        self.assertNotIn("reference_instructions", skill["work_order"])

    def test_release_only_links_complete_matched_artifacts(self):
        pack = runtime.get_pack("travel-rebooking")
        skill = runtime.skill_record(pack)
        skill.update({"id": "k1", "version": 1})
        server.initialize_skill_record(skill)
        skill["last_compare_budget"] = 1
        candidate = server.candidate_record(skill, {
            "instructions": copy.deepcopy(pack["skill"]["reference_instructions"]),
            "rationale": [],
        }, "owner")
        candidate["input_manifest"] = {
            "source_evidence": [{"id": "source-current"}],
        }
        skill["candidate"] = candidate
        server.STATE["skills"]["k1"] = skill
        server.STATE["active"] = "k1"

        snapshot = runtime.case_snapshot(pack, "travel-incident")
        snapshot.update({"id": "s1", "skill": "k1"})
        server.STATE["snapshots"]["s1"] = snapshot
        server.STATE["situations"].append({
            "id": "t1", "skill": "k1", "sid": "s1", "disposition": "change",
            "commitment": "在固定承诺前抵达", "criterion": {
                "form": "fact", "spec": {"key": "on_time_for_commitment",
                                             "op": "==", "value": True}},
        })

        common = {"skill": "k1", "sid": "s1", "snapshot_hash": snapshot["world_hash"]}
        server.STATE["runs"] = [
            dict(common, id="base-full", variant={}, artifact_hash=skill["content_hash"]),
            dict(common, id="base-probe", variant={"mask": [5]}, artifact_hash="masked"),
            dict(common, id="candidate-full", variant={"draft": True},
                 artifact_hash=candidate["content_hash"]),
            dict(common, id="candidate-block", variant={"draft": True, "mask": [4]},
                 artifact_hash="candidate-masked"),
            dict(common, id="wrong-world", variant={"draft": True},
                 artifact_hash=candidate["content_hash"], snapshot_hash="different"),
        ]
        server.STATE["probes"] = [
            {"id": "source-old", "skill": "k1", "kind": "delete"},
            {"id": "source-current", "skill": "k1", "kind": "invert"},
            {"id": "block-current", "skill": "k1", "kind": "block",
             "case_id": "travel-incident",
             "baseline_artifact_hash": candidate["content_hash"]},
            {"id": "block-old", "skill": "k1", "kind": "block",
             "case_id": "travel-incident", "baseline_artifact_hash": "old-candidate"},
        ]

        evidence = server.release_evidence(skill, candidate)
        case = evidence["cases"][0]
        self.assertEqual(["base-full"], case["baseline_runs"])
        self.assertEqual(["candidate-full"], case["candidate_runs"])
        self.assertTrue(case["evaluator_hash"])
        self.assertEqual(snapshot["world_hash"], case["world_hash"])
        self.assertEqual(runtime.RUNTIME_SCHEMA, evidence["runtime"])
        self.assertEqual(["source-current"], evidence["source_interventions"])
        self.assertEqual(["block-current"], evidence["candidate_interventions"])

        self.assertIn("全部已记录情况", server.release_readiness(skill, [], ""))
        clean = [{"situation_id": "t1", "expectation": "在固定承诺前抵达",
                  "disposition": "change",
                  "conflict": False, "needs_judgment": False, "insufficient": False}]
        self.assertIsNone(server.release_readiness(skill, clean, ""))
        warning = [dict(clean[0], needs_judgment=True)]
        self.assertIn("发布理由", server.release_readiness(skill, warning, ""))
        self.assertIsNone(server.release_readiness(skill, warning, "owner waiver"))
        failed = [dict(clean[0], execution_failed=True)]
        self.assertIn("执行失败", server.release_readiness(skill, failed, "owner waiver"))

    def test_chat_owner_edit_commits_exact_candidate(self):
        pack = runtime.get_pack("expense-review")
        skill = runtime.skill_record(pack)
        skill.update({"id": "k2", "version": 1})
        server.initialize_skill_record(skill)
        server.STATE["skills"]["k2"] = skill
        server.STATE["active"] = "k2"
        skill["candidate"] = server.candidate_record(skill, {
            "instructions": copy.deepcopy(skill["instructions"]), "rationale": [],
        }, "ai")
        replacement = "先检查受限类别，再决定是否可以进入金额快速通道。"

        result = server.op_edit_instruction(5, replacement)

        self.assertTrue(result["edited"])
        self.assertEqual("owner", skill["candidate"]["author"])
        edited = next(row for row in skill["candidate"]["instructions"] if row["n"] == 5)
        self.assertEqual(replacement, edited["text"])
        self.assertEqual(server.full_hash(skill["candidate"]["instructions"]),
                         skill["candidate"]["content_hash"])

    def test_owner_edit_cannot_bypass_scope_and_source_preview(self):
        pack = runtime.get_pack("expense-review")
        skill = runtime.skill_record(pack)
        skill.update({"id": "k-edit-guard", "version": 1})
        server.initialize_skill_record(skill)
        server.STATE["skills"][skill["id"]] = skill
        server.STATE["active"] = skill["id"]

        result = server.op_edit_instruction(5, "先检查品类，再处理票据。")

        self.assertIn("适用范围", result["error"])
        self.assertIsNone(skill["candidate"])

    def test_excluded_case_is_monitored_but_not_sent_as_a_normative_commitment(self):
        skill = server.initialize_skill_record({
            "id": "k-excluded", "name": "test", "version": 1,
            "instructions": [{"n": 1, "text": "do work"}], "tools": [],
            "sources": {}, "config": {}, "candidate": None,
        })
        server.STATE["skills"][skill["id"]] = skill
        server.STATE["active"] = skill["id"]
        server.STATE["snapshots"]["s-visible"] = {
            "id": "s-visible", "skill": skill["id"], "case_id": "visible", "task": "visible"}
        server.STATE["snapshots"]["s-excluded"] = {
            "id": "s-excluded", "skill": skill["id"], "case_id": "excluded", "task": "excluded"}
        server.STATE["situations"].extend([
            {"id": "t-visible", "skill": skill["id"], "sid": "s-visible",
             "disposition": "change", "commitment": "change this"},
            {"id": "t-excluded", "skill": skill["id"], "sid": "s-excluded",
             "disposition": "excluded", "commitment": "outside this repair"},
        ])

        manifest = server.compile_manifest(skill)

        self.assertEqual(["t-visible"],
                         [row["id"] for row in manifest["visible_commitments"]])
        self.assertEqual(["t-excluded"],
                         [row["id"] for row in manifest["excluded_cases"]])
        candidate = server.candidate_record(skill, {
            "instructions": copy.deepcopy(skill["instructions"]), "rationale": []},
            "ai", manifest)
        excluded = next(row for row in candidate["case_exposure"]
                        if row["situation_id"] == "t-excluded")
        self.assertEqual("withheld", excluded["candidate_author"])
        self.assertEqual("excluded-case-triage", excluded["scope_role"])

    def test_workspace_revision_archives_matched_feedback_for_redrafting(self):
        pack = runtime.get_pack("travel-rebooking")
        skill = runtime.skill_record(pack)
        skill.update({"id": "k-revise-feedback", "version": 1})
        server.initialize_skill_record(skill)
        server.STATE["skills"][skill["id"]] = skill
        server.STATE["active"] = skill["id"]
        skill["candidate"] = server.candidate_record(skill, {
            "instructions": copy.deepcopy(pack["skill"]["reference_instructions"]),
            "rationale": [],
        }, "ai")
        skill["last_compare"] = server.comparison_from_outcome([{
            "situation_id": "t1", "case_id": "travel-incident",
            "expectation": "arrive on time", "disposition": "change", "conflict": True,
        }])

        result = server.begin_candidate_revision(skill)

        self.assertFalse(result.get("error"))
        self.assertTrue(result["archived_candidate_id"].startswith("v"))
        self.assertEqual("failed", skill["pending_candidate_feedback"][0]["result"])

    def test_chat_preserve_verdict_uses_the_same_majority_rule_as_workspace(self):
        pack = runtime.get_pack("expense-review")
        skill = runtime.skill_record(pack)
        skill.update({"id": "k-majority", "version": 1})
        server.initialize_skill_record(skill)
        skill["candidate"] = server.candidate_record(skill, {
            "instructions": copy.deepcopy(pack["skill"]["reference_instructions"]),
            "rationale": [],
        }, "owner")
        server.STATE["skills"][skill["id"]] = skill
        server.STATE["active"] = skill["id"]
        snapshot = runtime.case_snapshot(pack, "expense-missing-field-preserve")
        snapshot.update({"id": "s-majority", "skill": skill["id"]})
        server.STATE["snapshots"][snapshot["id"]] = snapshot
        server.STATE["situations"].append({
            "id": "t-majority", "skill": skill["id"], "sid": snapshot["id"],
            "case_id": snapshot["case_id"], "disposition": "preserve",
            "commitment": "字段缺失时仍应退回且不得入账",
            "criterion": {"form": "fact", "spec": {
                "key": "ledger_entry_created", "op": "==", "value": False}},
            "candidate_outcome_revealed_at": None,
        })
        summaries = [
            {"top": "kept", "share": 1.0, "pass": 3, "tot": 3},
            {"top": "regressed", "share": 0.67, "pass": 1, "tot": 3},
        ]

        with mock.patch.object(server, "execute_batch", return_value=[]), \
                mock.patch.object(server, "summarize", side_effect=summaries):
            result = server.op_compare(3)

        self.assertEqual("broken", result["rows"][0]["verdict"])

    def test_candidate_generation_retries_a_noop_change_patch(self):
        original = [{"n": 1, "text": "优先选择价格最低的方案。"}]
        changed = [{"n": 1, "text": "存在固定承诺时先保证按时抵达，再比较价格。"}]
        replies = [
            {"instructions": copy.deepcopy(original), "rationale": []},
            {"instructions": copy.deepcopy(changed), "rationale": []},
        ]
        with mock.patch.object(server, "ask", side_effect=replies) as ask_mock:
            result = server.generate_candidate(original, [{
                "id": "t1", "disposition": "change", "expectation": "保证按时抵达",
            }], [])

        self.assertEqual(changed, result["instructions"])
        self.assertEqual(2, result["_generation_validation"]["attempts"])
        self.assertIn("VALIDATION ERROR", ask_mock.call_args_list[1].args[1])

    def test_candidate_generation_does_not_accept_repeated_numbers_as_a_change(self):
        original = [{"n": 1, "text": "优先选择价格最低的方案。"}]
        formatting_only = [{"n": 1, "text": "1. 1. 优先选择价格最低的方案。"}]
        changed = [{"n": 1, "text": "1. 存在固定承诺时先保证按时抵达，再比较价格。"}]
        with mock.patch.object(server, "ask", side_effect=[
                {"instructions": formatting_only, "rationale": []},
                {"instructions": changed, "rationale": []}]) as ask_mock:
            result = server.generate_candidate(original, [{
                "id": "t1", "disposition": "change", "expectation": "保证按时抵达",
            }], [])

        self.assertEqual(2, ask_mock.call_count)
        self.assertEqual("存在固定承诺时先保证按时抵达，再比较价格。",
                         result["instructions"][0]["text"])

    def test_candidate_generation_replaces_a_source_located_conflict(self):
        original = [{"n": 4, "text": "固定日程不改变排序。"}]
        appended = [{"n": 4, "text":
                     "固定日程不改变排序。但存在固定日程时优先保证按时抵达。"}]
        replaced = [{"n": 4, "text":
                     "存在固定日程时优先保证按时抵达，再比较价格。"}]
        evidence = [{"id": "p1", "instruction": 4, "changed": True}]
        with mock.patch.object(server, "ask", side_effect=[
                {"instructions": appended, "rationale": []},
                {"instructions": replaced, "rationale": []}]) as ask_mock:
            result = server.generate_candidate(original, [{
                "id": "t1", "disposition": "change", "expectation": "保证按时抵达",
            }], evidence)

        self.assertEqual(2, ask_mock.call_count)
        self.assertEqual(replaced, result["instructions"])
        self.assertIn("逐字保留旧条款", ask_mock.call_args_list[1].args[1])

    def test_candidate_generation_recovers_repeated_noop_with_targeted_edit(self):
        original = [{"n": 1, "text": "读取日程。"},
                    {"n": 2, "text": "选择价格最低的方案。"}]
        unchanged = {"instructions": copy.deepcopy(original), "rationale": []}
        targeted = {"n": 2, "text": "存在固定承诺时先保证按时抵达，再比较价格。",
                    "why": "落实固定承诺优先", "commitment_ids": ["t1"],
                    "source_evidence_ids": ["p1"]}
        with mock.patch.object(server, "ask", side_effect=[
                copy.deepcopy(unchanged), copy.deepcopy(unchanged),
                copy.deepcopy(unchanged), targeted]) as ask_mock:
            result = server.generate_candidate(original, [{
                "id": "t1", "disposition": "change", "expectation": "固定承诺优先",
            }], [{"id": "p1", "instruction": 2, "changed": True}])

        self.assertEqual(4, ask_mock.call_count)
        self.assertEqual("targeted-edit-after-noop",
                         result["_generation_validation"]["recovery"])
        self.assertEqual(targeted["text"], result["instructions"][1]["text"])

    def test_scenario_scope_readiness_requires_every_product_case(self):
        pack = runtime.get_pack("travel-rebooking")
        skill = runtime.skill_record(pack)
        skill.update({"id": "k3", "version": 1})
        server.initialize_skill_record(skill)
        server.STATE["skills"]["k3"] = skill
        server.STATE["active"] = "k3"

        readiness = server.scope_readiness(skill)
        self.assertFalse(readiness["ready"])
        self.assertEqual(4, len(readiness["missing"]))

        for index, case_id in enumerate(readiness["required_case_ids"]):
            snapshot = runtime.case_snapshot(pack, case_id)
            snapshot.update({"id": "s%d" % index, "skill": "k3"})
            server.STATE["snapshots"][snapshot["id"]] = snapshot
            server.STATE["situations"].append({
                "id": "t%d" % index, "skill": "k3", "sid": snapshot["id"],
                "case_id": case_id, "pre_reveal": True,
                "candidate_outcome_revealed_at": None,
            })
        self.assertTrue(server.scope_readiness(skill)["ready"])
        progress = server.chat_progress(skill)
        self.assertEqual(4, progress["completed_cases"])
        self.assertEqual(4, progress["required_cases"])
        self.assertEqual(0, progress["remaining_cases"])
        self.assertEqual("正在准备候选版本并检查修改影响。", progress["next_action"])
        self.assertEqual(progress["next_action"], server.chat_guidance_text(progress))

    def test_chat_keeps_full_related_case_bank_after_opening_a_neighbour(self):
        pack = runtime.get_pack("travel-rebooking")
        skill = runtime.skill_record(pack)
        skill.update({"id": "kc", "version": 1})
        server.initialize_skill_record(skill)
        server.STATE["skills"]["kc"] = skill
        server.STATE["active"] = "kc"
        incident = runtime.case_snapshot(pack, "travel-incident")
        incident.update({"id": "s1", "skill": "kc"})
        neighbour = runtime.case_snapshot(pack, "travel-routine-preserve")
        neighbour.update({"id": "s2", "skill": "kc"})
        server.STATE["snapshots"].update({"s1": incident, "s2": neighbour})

        result = server.op_suggest_cases()

        self.assertEqual("frozen-case-bank", result["source"])
        self.assertEqual(3, len(result["cases"]))

    def test_scope_plan_selects_frozen_cases_from_owner_intent(self):
        pack = runtime.get_pack("expense-review")
        skill = runtime.skill_record(pack)
        skill.update({"id": "kp", "version": 1})
        server.initialize_skill_record(skill)
        server.STATE["skills"]["kp"] = skill
        server.STATE["active"] = "kp"
        incident = runtime.case_snapshot(pack, "expense-incident")
        incident.update({"id": "s1", "skill": "kp"})
        server.STATE["snapshots"]["s1"] = incident
        planned = {
            "intent": {
                "summary": "受限品类必须先经主管审批，批准前不得入账。",
                "trigger": "品类属于受限清单",
                "required_action": "发起主管审批",
                "forbidden_action": "批准前创建财务凭证",
                "ambiguities": ["未列入清单的品类是否也算不合规"],
            },
            "cases": [{
                "case_id": "expense-routine-preserve",
                "relation_type": "outside-trigger",
                "why_relevant": "确认规则不会扩大到明确合规的票据。",
                "owner_question": "合规票据是否继续自动入账？",
            }, {
                "case_id": "expense-client-gift-unresolved",
                "relation_type": "definition-boundary",
                "why_relevant": "澄清不合规是否只指清单中的受限品类。",
                "owner_question": "未列入清单的客户礼品是否也要审批？",
            }],
        }

        with mock.patch.object(server, "ask", return_value=planned):
            plan = server.ensure_scope_plan(
                skill, "品类不合规时不应自动入账，需要管理员审核")

        self.assertEqual(["expense-routine-preserve", "expense-client-gift-unresolved"],
                         [row["case_id"] for row in plan["cases"]])
        self.assertEqual("intent-conditioned-frozen-case-bank", plan["source"])
        self.assertEqual(plan["id"], skill["active_scope_plan_id"])
        self.assertEqual(3, len(server.scope_readiness(skill)["required_case_ids"]))
        rendered = server.related_review_text(server.related_case_contexts(skill),
                                               intent=plan["intent"])
        self.assertIn("受限品类必须先经主管审批", rendered)
        self.assertIn("与这条规则的关系", rendered)
        self.assertIn("未列入清单的客户礼品是否也要审批", rendered)
        manifest = server.compile_manifest(skill, "chat")
        self.assertEqual(plan["hash"], manifest["scope_plan_hash"])
        self.assertNotIn("scope_plan_intent", manifest)

    def test_formal_scope_plan_keeps_the_calibrated_case_contract(self):
        pack = runtime.get_pack("expense-review")
        skill = runtime.skill_record(pack)
        skill.update({"id": "kpf", "version": 1,
                      "study_context": {"formal": True, "condition": "chat"}})
        server.initialize_skill_record(skill)
        server.STATE["skills"]["kpf"] = skill
        server.STATE["active"] = "kpf"
        incident = runtime.case_snapshot(pack, "expense-incident")
        incident.update({"id": "s1", "skill": "kpf"})
        server.STATE["snapshots"]["s1"] = incident
        planned = {"intent": {"summary": "受限品类先审批。"}, "cases": [{
            "case_id": "expense-client-gift-unresolved",
            "relation_type": "definition-boundary",
            "why_relevant": "澄清受限定义。", "owner_question": "是否包含未列明品类？",
        }]}

        with mock.patch.object(server, "ask", return_value=planned):
            plan = server.ensure_scope_plan(skill, "受限品类先审批")

        self.assertEqual(3, len(plan["cases"]))
        self.assertEqual(4, len(server.scope_readiness(skill)["required_case_ids"]))

    def test_automatic_repair_preview_is_frozen_into_the_manifest(self):
        pack = runtime.get_pack("expense-review")
        skill = runtime.skill_record(pack)
        skill.update({"id": "k-preview", "version": 1})
        server.initialize_skill_record(skill)
        server.STATE["skills"][skill["id"]] = skill
        server.STATE["active"] = skill["id"]
        case_ids = [pack["entry_case"]] + [
            row["case_id"] for row in runtime.get_case(pack, pack["entry_case"])["neighbours"]]
        dispositions = ["change", "preserve", "preserve", "unresolved"]
        for index, (case_id, disposition) in enumerate(zip(case_ids, dispositions)):
            snapshot = runtime.case_snapshot(pack, case_id)
            snapshot.update({"id": "s-preview-%d" % index, "skill": skill["id"]})
            server.STATE["snapshots"][snapshot["id"]] = snapshot
            server.STATE["situations"].append({
                "id": "t-preview-%d" % index, "skill": skill["id"],
                "sid": snapshot["id"], "case_id": case_id,
                "disposition": disposition, "commitment": "owner commitment %d" % index,
                "criterion": None, "sealed": index == 1, "pre_reveal": True,
                "judged_before_candidate_in_round": True, "review_round": 1,
            })
        skill["scope_version"] = 2
        server.freeze_scope_version(skill)
        incident = server._snapshot_for_case(pack["entry_case"])
        for index in range(3):
            server.STATE["runs"].append({
                "id": "r-base-%d" % index, "skill": skill["id"], "sid": incident["id"],
                "variant": {}, "artifact_hash": skill["content_hash"],
                "facts": {"decision": "approve", "ledger_entry_created": True},
                "steps": [], "outcome": "approved",
            })

        planned = {"probe": {"instruction": 5, "case_id": pack["entry_case"],
                              "question": ("If this source instruction is temporarily removed or "
                                           "minimally inverted, does the behavior change?"),
                              "commitment_ids": ["t-preview-0"]}}
        inverted = {"text": "金额不高于日常额度时，不得直接通过。"}
        call_index = {"value": 0}

        def fake_batch(instructions, snapshot, variant, k, criterion=None):
            call_index["value"] += 1
            changed = bool(variant.get("rewrite"))
            return [{"id": "r-probe-%d-%d" % (call_index["value"], i),
                     "facts": {"decision": "escalate" if changed else "approve",
                               "ledger_entry_created": not changed},
                     "steps": [], "outcome": "probe"} for i in range(k)]

        with mock.patch.object(server, "ask", side_effect=[planned, inverted]), \
                mock.patch.object(server, "execute_batch", side_effect=fake_batch):
            preview = server.ensure_repair_preview(skill)

        self.assertEqual(5, preview["instruction"])
        self.assertEqual(2, len(preview["evidence_ids"]))
        public_preview = server.public_repair_preview(skill)
        self.assertEqual({"delete", "invert"},
                         {row["kind"] for row in public_preview["evidence"]})
        self.assertEqual("related", public_preview["assessment"]["status"])
        self.assertNotIn("temporarily removed", public_preview["question"])
        self.assertNotIn("minimally inverted", public_preview["question"])
        participant_text = server.repair_preview_text(skill, preview)
        self.assertIn("相关修改依据", participant_text)
        self.assertIn("为什么建议这里", participant_text)
        self.assertIn("不需要单独确认", participant_text)
        self.assertNotIn("临时移除", participant_text)
        self.assertNotIn("最小反转", participant_text)
        manifest = server.compile_manifest(skill, "workspace")
        self.assertEqual(preview["id"], manifest["repair_preview_id"])
        self.assertEqual(preview["hash"], manifest["repair_preview_hash"])
        self.assertEqual(set(preview["evidence_ids"]),
                         {row["id"] for row in manifest["source_evidence"]})

    def test_post_reveal_scope_revision_starts_a_new_auditable_round(self):
        pack = runtime.get_pack("travel-rebooking")
        skill = runtime.skill_record(pack)
        skill.update({"id": "k-round", "version": 1, "first_candidate_revealed_at": 10.0})
        server.initialize_skill_record(skill)
        server.STATE["skills"][skill["id"]] = skill
        server.STATE["active"] = skill["id"]
        case_ids = [pack["entry_case"]] + [
            row["case_id"] for row in runtime.get_case(pack, pack["entry_case"])["neighbours"]]
        for index, case_id in enumerate(case_ids):
            snapshot = runtime.case_snapshot(pack, case_id)
            snapshot.update({"id": "s-round-%d" % index, "skill": skill["id"]})
            server.STATE["snapshots"][snapshot["id"]] = snapshot
            server.STATE["situations"].append({
                "id": "t-round-%d" % index, "skill": skill["id"], "sid": snapshot["id"],
                "case_id": case_id, "disposition": "change" if index == 0 else
                "unresolved" if index == 3 else "preserve",
                "commitment": "round one %d" % index, "pre_reveal": True,
                "judged_before_candidate_in_round": True, "review_round": 1,
                "candidate_outcome_revealed_at": 11.0,
            })
        skill["candidate"] = server.candidate_record(skill, {
            "instructions": copy.deepcopy(pack["skill"]["reference_instructions"]),
            "rationale": [],
        }, "owner")
        skill["last_compare"] = [{"expectation": "round one 0", "disposition": "change",
                                   "verdict": "met", "case_id": case_ids[0]}]

        result = server.begin_scope_revision(skill)

        self.assertEqual(2, result["review_round"])
        self.assertIsNone(skill["candidate"])
        self.assertTrue(skill["scope_revision_required"])
        self.assertEqual(1, len(skill["candidate_rounds"]))
        active = server.scope_items(skill["id"])
        self.assertEqual(4, len(active))
        self.assertTrue(all(row["review_round"] == 2 for row in active))
        self.assertTrue(all(row["post_reveal"] for row in active))
        self.assertTrue(server.scope_readiness(skill)["ready"])

    def test_keep_alive_requests_do_not_reuse_previous_json_body(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        try:
            for name in ("first", "second"):
                connection.request("POST", "/api/event", json.dumps({"name": name}), headers)
                response = connection.getresponse()
                response.read()
                self.assertEqual(200, response.status)
            connection.request("GET", "/api/state")
            response = connection.getresponse()
            response.read()
            self.assertEqual(200, response.status)
            self.assertEqual(["first", "second"],
                             [row["name"] for row in server.STATE["events"]])
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_run_endpoint_rejects_non_numeric_batch_size(self):
        original_key = server.API_KEY
        server.API_KEY = "test-key"
        server.STATE["skills"]["k1"] = {"id": "k1", "instructions": []}
        server.STATE["active"] = "k1"
        server.STATE["snapshots"]["s1"] = {"id": "s1", "skill": "k1"}

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        try:
            connection.request("POST", "/api/run",
                               json.dumps({"snapshot": "s1", "k": {}}),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(400, response.status)
            self.assertIn("1 到 8", body["error"])
            self.assertEqual([], server.STATE["runs"])
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            server.API_KEY = original_key

    def test_workspace_compare_click_does_not_forward_dom_event(self):
        source = (server.HERE / "app.html").read_text(encoding="utf-8")
        self.assertIn("onclick:function(){compare();}", source)
        self.assertNotIn("onclick:compare}", source)

    def test_mode_switches_are_bidirectional_and_preserve_study_lock(self):
        workspace = (server.HERE / "app.html").read_text(encoding="utf-8")
        chat = (server.HERE / "chat.html").read_text(encoding="utf-8")
        self.assertIn("[hidden]{display:none!important}", workspace)
        self.assertIn("[hidden]{display:none!important}", chat)
        self.assertIn('id="modeSwitch" href="/chat"', workspace)
        self.assertIn('id="modeSwitch" href="/"', chat)
        self.assertIn('modeParams.get("lockMode")==="1"', workspace)
        self.assertIn('modeParams.get("lockMode")==="1"', chat)
        self.assertIn('track("mode_switched"', chat)
        self.assertIn('track("mode_switched"', workspace)

    def test_both_conditions_use_the_same_participant_work_order_contract(self):
        workspace = (server.HERE / "app.html").read_text(encoding="utf-8")
        chat = (server.HERE / "chat.html").read_text(encoding="utf-8")
        for source in (workspace, chat):
            self.assertIn('id="workOrderButton"', source)
            self.assertIn('id="workOrderLayer"', source)
            self.assertIn("function formalAssignmentProblem(st)", source)
            self.assertIn("!STUDY_META.participant||!STUDY_META.period", source)
            self.assertIn('section("事件背景",order.context)', source)
            self.assertIn('section("你的目标",order.objective)', source)
            self.assertIn('section("完成条件",order.completion)', source)
            self.assertIn('"work_order_accepted"', source)
            self.assertNotIn("innerHTML", source)
        self.assertIn('condition:"workspace"', workspace)
        self.assertIn('condition:"chat"', chat)
        self.assertIn('ctx.condition!=="workspace"', workspace)
        self.assertIn('ctx.condition!=="chat"', chat)
        self.assertIn('if(FORMAL||modeParams.get("lockMode")==="1")$("reset").hidden=true',
                      workspace)

    def test_scenario_load_freezes_formal_assignment_metadata(self):
        original_key = server.API_KEY
        server.API_KEY = "test-key"
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        body = {"id": "travel-rebooking", "study": {
            "session": "session-a", "participant": "P07", "condition": "chat",
            "period": "2", "formal": True,
        }}
        try:
            connection.request("POST", "/api/scenario/load", json.dumps(body),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(200, response.status)
            order = payload["work_order"]
            context = payload["study_context"]
            self.assertEqual("travel-rebooking-review-v1", order["id"])
            self.assertEqual(order["task_hash"], context["task_hash"])
            self.assertEqual("P07", context["participant"])
            self.assertEqual("chat", context["condition"])
            self.assertTrue(context["formal"])
            self.assertEqual(context["started_at"], context["brief_acknowledged_at"])
            self.assertEqual(order, server.cur()["work_order"])
            self.assertEqual(context, server.cur()["study_context"])

            active_id = server.STATE["active"]
            connection.request("POST", "/api/scenario/load", json.dumps(body),
                               {"Content-Type": "application/json"})
            duplicate = connection.getresponse()
            duplicate_payload = json.loads(duplicate.read())
            self.assertEqual(409, duplicate.status)
            self.assertIn("隔离实例", duplicate_payload["error"])
            self.assertEqual(active_id, server.STATE["active"])

            incomplete = copy.deepcopy(body)
            incomplete["study"]["participant"] = ""
            connection.request("POST", "/api/scenario/load", json.dumps(incomplete),
                               {"Content-Type": "application/json"})
            invalid = connection.getresponse()
            invalid_payload = json.loads(invalid.read())
            self.assertEqual(400, invalid.status)
            self.assertIn("participant", invalid_payload["error"])
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            server.API_KEY = original_key

    def test_chat_restores_same_session_without_executable_html(self):
        source = (server.HERE / "chat.html").read_text(encoding="utf-8")
        self.assertIn("/api/chat/bootstrap?session=", source)
        self.assertIn("正在恢复当前评审", source)
        self.assertIn("function bootstrapText(", source)
        self.assertIn("function messageBody(", source)
        self.assertNotIn("innerHTML", source)

    def test_chat_uses_on_demand_skill_document_and_demo_reset(self):
        source = (server.HERE / "chat.html").read_text(encoding="utf-8")
        self.assertIn('id="skillButton"', source)
        self.assertIn('id="skillLayer"', source)
        self.assertIn('id="reset"', source)
        self.assertIn("function appendSkillMessage(", source)
        self.assertIn("function showSkillDrawer(", source)
        self.assertIn("function resetChat(", source)
        self.assertIn('title="清空本轮评审并返回工作流选择"', source)
        self.assertIn('if(FORMAL||modeParams.get("lockMode")==="1"){', source)
        self.assertIn('$("reset").hidden=true', source)
        self.assertNotIn('class="side"', source)
        self.assertNotIn("innerHTML", source)

    def test_source_location_mechanics_are_disclosed_in_product_language(self):
        workspace = (server.HERE / "app.html").read_text(encoding="utf-8")
        chat = (server.HERE / "chat.html").read_text(encoding="utf-8")
        self.assertIn("相关修改依据", workspace)
        self.assertIn("生成候选修改", workspace)
        self.assertIn("查看系统如何判断", workspace)
        self.assertIn("不应用这条规则", workspace)
        self.assertIn("采用相反处理原则", workspace)
        self.assertIn("function appendRepairCheckDetails", chat)
        self.assertIn("查看系统如何判断", chat)
        for source in (workspace, chat):
            self.assertNotIn("临时移除", source)
            self.assertNotIn("最小反转", source)
            self.assertNotIn("确认依据", source)

    def test_conditions_share_automatic_case_and_candidate_checks(self):
        workspace = (server.HERE / "app.html").read_text(encoding="utf-8")
        chat = (server.HERE / "chat.html").read_text(encoding="utf-8")
        self.assertIn('post("/api/prepare_cases"', workspace)
        self.assertIn('setTimeout(function(){compare();}', workspace)
        self.assertIn("draft_and_compare()", inspect.getsource(server.Handler.h_api_chat))
        self.assertNotIn("确认修改位置", workspace)
        self.assertIn("相关修改依据", workspace)
        self.assertIn("相关修改依据", chat)

    def test_chat_bootstrap_is_grounded_and_excludes_private_results(self):
        pack = runtime.get_pack("travel-rebooking")
        skill = runtime.skill_record(pack)
        skill.update({"id": "kb", "version": 1})
        server.initialize_skill_record(skill)
        server.STATE["skills"]["kb"] = skill
        server.STATE["active"] = "kb"
        snapshot = runtime.case_snapshot(pack, "travel-incident")
        snapshot.update({"id": "s1", "skill": "kb", "baseline_completed_at": 1})
        server.STATE["snapshots"]["s1"] = snapshot
        server.STATE["runs"].append({
            "id": "r1", "skill": "kb", "sid": "s1", "variant": {},
            "outcome": "已选择 UA1123 并完成改签。",
            "facts": {"selected_flight": "UA1123",
                      "arrival_time": "2026-03-13T14:20:00-05:00",
                      "price": 180, "on_time_for_commitment": False},
            "_oracle": {"all_required_passed": False},
        })
        server.STATE["situations"].append({
            "id": "t1", "skill": "kb", "sid": "s1", "case_id": "travel-incident",
            "disposition": "change", "commitment": "固定日程优先",
            "pre_reveal": True, "candidate_outcome_revealed_at": None,
        })

        payload = server.public_chat_bootstrap("session-a")
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertTrue(payload["active"])
        self.assertEqual("travel-rebooking-review-v1", payload["work_order"]["id"])
        self.assertEqual(payload["work_order"]["task_hash"],
                         skill["work_order"]["task_hash"])
        self.assertEqual("UA1123", payload["incident"]["facts"]["selected_flight"])
        self.assertEqual("execution-derived", payload["incident"]["issue"]["source"])
        self.assertEqual(1, len(payload["scope"]))
        self.assertEqual(len(skill["instructions"]), len(payload["skill"]["instructions"]))
        self.assertEqual(len(skill["tools"]), len(payload["skill"]["tools"]))
        self.assertEqual(1, payload["progress"]["completed_cases"])
        self.assertEqual(4, payload["progress"]["required_cases"])
        self.assertEqual(3, payload["progress"]["remaining_cases"])
        self.assertEqual("剩余 3 种情况需要确认。", payload["progress"]["next_action"])
        self.assertNotIn("_oracle", serialized)
        self.assertNotIn("research-holdout", serialized)
        self.assertNotIn("reference", serialized.lower())

    def test_chat_reply_explains_the_decision_without_exposing_commands(self):
        pack = runtime.get_pack("expense-review")
        skill = runtime.skill_record(pack)
        skill.update({"id": "kg", "version": 1})
        server.initialize_skill_record(skill)
        server.STATE["skills"]["kg"] = skill
        server.STATE["active"] = "kg"

        class FakeHandler:
            chunks = []

            def _body(self):
                return {"message": "我想先理解当前情况", "history": [],
                        "session": "session-guidance"}

            def _open(self):
                pass

            def _chunk(self, value):
                self.chunks.append(value)

            def _close(self):
                pass

        handler = FakeHandler()
        with mock.patch.object(server, "ask", side_effect=[
                {"capability": "none", "args": {}}, {"reply": "我没有执行新的操作。"}]):
            server.Handler.h_api_chat(handler)

        text_chunk = next(row for row in handler.chunks if row.get("type") == "text")
        self.assertIn("这类情况以后应当怎样处理", text_chunk["text"])
        self.assertNotIn("操作口令", text_chunk["text"])
        self.assertNotIn("固定格式", text_chunk["text"])
        self.assertNotIn("由我完成", text_chunk["text"])
        self.assertNotIn("请先请求", text_chunk["text"])
        self.assertEqual(0, server.STATE["chat"][-1]["progress"]["completed_cases"])
        self.assertEqual(4, server.STATE["chat"][-1]["progress"]["required_cases"])

    def test_case_bound_expectation_replaces_a_duplicate_instead_of_appending(self):
        pack = runtime.get_pack("travel-rebooking")
        skill = runtime.skill_record(pack)
        skill.update({"id": "kr", "version": 1})
        server.initialize_skill_record(skill)
        server.STATE["skills"]["kr"] = skill
        server.STATE["active"] = "kr"
        snapshot = runtime.case_snapshot(pack, "travel-incident")
        snapshot.update({"id": "s1", "skill": "kr"})
        server.STATE["snapshots"]["s1"] = snapshot
        server.STATE["runs"].append({
            "id": "r1", "skill": "kr", "sid": "s1", "variant": {},
            "facts": {"on_time_for_commitment": False}, "steps": [],
            "outcome": "晚于固定客户汇报抵达。",
        })
        criterion = {"candidates": [{"label": "按时抵达", "form": "fact",
                                      "spec": {"key": "on_time_for_commitment",
                                               "op": "==", "value": True}}]}
        with mock.patch.object(server, "ask", return_value=criterion):
            first = server.op_expect("固定客户汇报前抵达", "change", "travel-incident")
            second = server.op_expect("不可错过固定客户汇报", "change", "travel-incident")

        self.assertFalse(first["replaced"])
        self.assertTrue(second["replaced"])
        self.assertEqual(1, len(server.scope_items("kr")))
        self.assertEqual("不可错过固定客户汇报",
                         server.scope_items("kr")[0]["commitment"])
        self.assertEqual("on_time_for_commitment",
                         server.scope_items("kr")[0]["criterion"]["spec"]["key"])

    def test_compact_travel_outcome_omits_a_fake_deadline_for_routine_cases(self):
        pack = runtime.get_pack("travel-rebooking")
        skill = runtime.skill_record(pack)
        case = runtime.get_case(pack, "travel-routine-preserve")
        text = server._compact_case_outcome(skill, {
            "selected_flight": "UA200", "price": 160,
            "on_time_for_commitment": True, "booking_completed": True,
            "confirmation_before_booking": False,
        }, case=case)

        self.assertIn("选择 UA200", text)
        self.assertNotIn("固定承诺", text)

    def test_one_natural_reply_can_accept_preserves_and_decide_the_open_boundary(self):
        cases = [
            {"case_id": "routine", "summary": "常规情况", "outcome": "保持低价",
             "proposal": {"disposition": "preserve", "commitment": "继续优先低价"},
             "question": ""},
            {"case_id": "confirm", "summary": "高额情况", "outcome": "先确认",
             "proposal": {"disposition": "preserve", "commitment": "高额操作前确认"},
             "question": ""},
            {"case_id": "extreme", "summary": "极端情况", "outcome": "自动处理",
             "proposal": {"disposition": "unresolved", "commitment": "交由人工判断"},
             "question": "是否制定自动规则？"},
        ]
        parsed = {"understood": True, "accept_proposals": True,
                  "decisions": [{"case_id": "extreme", "disposition": "unresolved",
                                 "commitment": "极端情况继续交由人工判断"}],
                  "clarification": ""}
        with mock.patch.object(server, "ask", return_value=parsed):
            result = server.extract_scope_decisions("前两项没问题，极端情况人工判断", cases)

        self.assertTrue(result["understood"])
        self.assertEqual({"routine", "confirm", "extreme"},
                         {row["case_id"] for row in result["decisions"]})
        self.assertEqual(3, len(result["decisions"]))

    def test_chat_composer_does_not_show_a_tool_command_catalog(self):
        source = (server.HERE / "chat.html").read_text(encoding="utf-8")
        self.assertNotIn('id="composerHint"', source)
        self.assertNotIn("操作命令", source)
        self.assertIn('placeholder="这类情况以后应当怎样处理？"', source)
        self.assertNotIn('class="caps"', source)
        self.assertIn("function appendCandidateMessage(", source)

    def test_post_task_questionnaire_hides_fact_keys_and_answers(self):
        pack = runtime.get_pack("expense-review")
        cases = server.prediction_holdouts(pack)
        record = {"id": "q1", "scenario_id": pack["id"], "status": "presented"}
        public = server.public_questionnaire(record, cases)
        self.assertEqual("expense-review-review-v1", public["work_order"]["id"])
        self.assertEqual(2, len(public["prediction_cases"]))
        self.assertEqual(6, len(public["prediction_questions"]))
        self.assertTrue(all(len(row["prediction_questions"]) == 3
                            for row in public["prediction_cases"]))
        self.assertEqual(4, len(public["case_brief"]))
        self.assertTrue(public["rating_items"])
        self.assertTrue(public["workload_items"])
        self.assertTrue(all("fact" not in row for row in public["prediction_questions"]))
        self.assertNotIn("oracle", public)
        questionnaire = (server.HERE / "questionnaire.html").read_text(encoding="utf-8")
        self.assertIn("/api/study/questionnaire/start", questionnaire)
        self.assertIn("/api/study/questionnaire/submit", questionnaire)
        self.assertNotIn("innerHTML", questionnaire)

    def test_questionnaire_scores_frozen_prediction_and_blind_holdouts_privately(self):
        pack = runtime.get_pack("expense-review")
        skill = runtime.skill_record(pack)
        skill.update({"id": "kq", "version": 2})
        server.initialize_skill_record(skill)
        server.STATE["skills"]["kq"] = skill
        server.STATE["active"] = "kq"
        cases = server.prediction_holdouts(pack)
        question_schema = []
        prediction_cases = []
        for case in cases:
            questions = copy.deepcopy(case["study_measure"]["prediction_questions"])
            for question in questions:
                question["case_id"] = case["id"]
            question_schema.extend(questions)
            prediction_cases.append({"case_id": case["id"],
                                     "case_hash": runtime.case_snapshot(
                                         pack, case["id"])["case_hash"]})
        record = {
            "id": "q1", "skill": "kq", "scenario_id": pack["id"],
            "case_id": cases[0]["id"], "prediction_cases": prediction_cases,
            "status": "presented",
            "artifact": {"hash": "frozen", "version": 2,
                         "instructions": copy.deepcopy(skill["instructions"])},
            "question_schema": question_schema,
        }
        server.STATE["questionnaires"].append(record)
        body = {
            "id": "q1",
            "predictions": [
                {"question_id": "decision", "value": "reject", "confidence": 90},
                {"question_id": "ledger", "value": False, "confidence": 80},
                {"question_id": "decision-recorded", "value": True, "confidence": 80},
                {"question_id": "restricted-decision", "value": "escalate", "confidence": 90},
                {"question_id": "restricted-manager", "value": True, "confidence": 90},
                {"question_id": "restricted-ledger", "value": False, "confidence": 90},
            ],
            "ratings": {row["id"]: 5 for row in server.POST_TASK_ITEMS},
            "workload": {row["id"]: 35 for row in server.RAW_TLX_ITEMS},
            "comment": "evidence was clear",
        }

        class FakeHandler:
            def _body(self):
                return body

            def _json(self, value, code=200):
                return {"status": code, "body": value}

        def fake_run(instructions, snapshot):
            if snapshot.get("case_id") == "expense-missing-field-holdout":
                facts = {"decision": "reject", "ledger_entry_created": False,
                         "decision_recorded": True}
            elif snapshot.get("case_id") == "expense-restricted-category-holdout":
                facts = {"decision": "escalate", "manager_approval_requested": True,
                         "ledger_entry_created": False}
            else:
                facts = {"decision": "escalate", "ledger_entry_created": False}
            return {"facts": facts, "_oracle": {"all_required_passed": True},
                    "execution": {"runtime": runtime.RUNTIME_SCHEMA}}

        with mock.patch.object(server, "_scenario_exec_once", side_effect=fake_run):
            response = server.Handler.h_api_study_questionnaire_submit(FakeHandler())

        self.assertEqual(200, response["status"])
        self.assertEqual({"ok", "completed", "measurement_status", "archive_saved"},
                         set(response["body"]))
        self.assertEqual(1.0, record["measurement"]["prediction_accuracy"])
        self.assertEqual(1.0, record["research_holdout"]["oracle_pass_rate"])
        self.assertEqual(6, len(record["holdout_runs"]))
        self.assertEqual(2, len(record["measurement"]["case_results"]))
        self.assertNotIn("confidence_brier", record["measurement"])
        self.assertEqual(3, len(record["research_holdout_runs"]))

    def test_workspace_uses_safe_structured_outcome_summaries(self):
        source = (server.HERE / "app.html").read_text(encoding="utf-8")
        self.assertIn("function outcomeSummary(", source)
        self.assertIn("function factComparison(", source)
        self.assertIn("function markdownBlock(", source)
        self.assertIn('h("details",{class:"outcome-detail"}', source)
        self.assertNotIn("innerHTML", source)

    def test_related_cases_stay_inside_one_repair_workspace(self):
        source = (server.HERE / "app.html").read_text(encoding="utf-8")
        self.assertIn("function reviewBarView(", source)
        self.assertIn('openCaseDetail(t,"scope-card")', source)
        self.assertIn('openCaseDetail(t,"scope-summary")', source)
        self.assertIn('x.snap.case_role==="incident"', source)
        self.assertNotIn('class:"tb"', source)
        self.assertNotIn('S.curBy[S.active]=item.taskId', source)

    def test_warning_release_uses_inline_decision_and_success_feedback(self):
        source = (server.HERE / "app.html").read_text(encoding="utf-8")
        self.assertIn("function releaseDecisionSlot(", source)
        self.assertIn("function releaseDecisionReady(", source)
        self.assertIn('track("release_warning_acknowledged"', source)
        self.assertIn('showNotice("发布成功"', source)
        self.assertIn('slot("发布成功"', source)
        self.assertIn("root.releaseReceipt=", source)
        self.assertIn("补充执行证据", source)
        self.assertNotIn('prompt("仍有 ', source)

    def test_import_sequence_recovery_ignores_domain_identifiers(self):
        exported = {"skill": {"id": "k1", "manifests": [{"id": "m31"}]},
                    "snapshots": [{"id": "s24"}],
                    "runs": [{"id": "r27", "selected": "UA1123"}],
                    "questionnaires": [{"id": "q34"}]}
        self.assertEqual(34, server.max_imported_sequence(exported))

    def test_reset_endpoint_clears_the_in_memory_workspace(self):
        original_key = server.API_KEY
        server.API_KEY = "test-key"
        server.STATE["skills"]["k1"] = {"id": "k1", "instructions": []}
        server.STATE["active"] = "k1"
        server.STATE["snapshots"]["s1"] = {"id": "s1", "skill": "k1"}
        for key in ("runs", "situations", "probes", "reviews", "events", "chat",
                    "questionnaires"):
            server.STATE[key].append({"id": key, "skill": "k1"})
        server.STATE["seq"] = 17

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        try:
            connection.request("POST", "/api/reset", "{}",
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            self.assertEqual(200, response.status)
            self.assertIsNone(server.STATE["active"])
            self.assertEqual({}, server.STATE["skills"])
            self.assertEqual({}, server.STATE["snapshots"])
            self.assertEqual(0, server.STATE["seq"])
            for key in ("runs", "situations", "probes", "reviews", "events", "chat",
                        "questionnaires"):
                self.assertEqual([], server.STATE[key])
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            server.API_KEY = original_key

    def test_issue_endpoint_waits_for_completed_baseline(self):
        original_key = server.API_KEY
        server.API_KEY = "test-key"
        pack = runtime.get_pack("travel-rebooking")
        skill = runtime.skill_record(pack)
        skill.update({"id": "k1", "version": 1})
        server.initialize_skill_record(skill)
        server.STATE["skills"]["k1"] = skill
        server.STATE["active"] = "k1"
        snapshot = runtime.case_snapshot(pack, "travel-incident")
        snapshot.update({"id": "s1", "skill": "k1"})
        server.STATE["snapshots"]["s1"] = snapshot
        server.STATE["runs"].append({
            "id": "r1", "skill": "k1", "sid": "s1", "variant": {},
            "facts": {"selected_flight": "UA1123",
                      "arrival_time": "2026-03-13T14:20:00-05:00",
                      "price": 180, "on_time_for_commitment": False},
        })

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        try:
            headers = {"Content-Type": "application/json"}
            connection.request("POST", "/api/issue", json.dumps({"snapshot": "s1"}), headers)
            response = connection.getresponse()
            response.read()
            self.assertEqual(409, response.status)

            snapshot["baseline_completed_at"] = 1
            connection.request("POST", "/api/issue", json.dumps({"snapshot": "s1"}), headers)
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(200, response.status)
            self.assertEqual("execution-derived", body["issue"]["source"])
            self.assertEqual("tool-trace-and-world-state", body["analysis"]["source"])
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            server.API_KEY = original_key


if __name__ == "__main__":
    unittest.main()
