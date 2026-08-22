import copy
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import scenario_runtime as runtime
import server
from scripts import study_assignments


EMPTY_STATE = {"skills": {}, "active": None, "snapshots": {}, "runs": [],
               "situations": [], "probes": [], "reviews": [], "events": [],
               "chat": [], "questionnaires": [], "seq": 0}


class StudyOperationsTests(unittest.TestCase):
    def setUp(self):
        self.original_state = server.STATE
        self.original_key = server.API_KEY
        self.original_data_dir = server.STUDY_DATA_DIR
        self.original_require_archive = server.REQUIRE_STUDY_ARCHIVE
        self.original_formal_assignment = server.FORMAL_ASSIGNMENT
        server.STATE = copy.deepcopy(EMPTY_STATE)
        server.API_KEY = "test-key"
        server.STUDY_DATA_DIR = None
        server.REQUIRE_STUDY_ARCHIVE = False
        server.FORMAL_ASSIGNMENT = {"participant": "", "period": "", "condition": "",
                                    "scenario_id": ""}

    def tearDown(self):
        server.STATE = self.original_state
        server.API_KEY = self.original_key
        server.STUDY_DATA_DIR = self.original_data_dir
        server.REQUIRE_STUDY_ARCHIVE = self.original_require_archive
        server.FORMAL_ASSIGNMENT = self.original_formal_assignment

    def request(self, path, body):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            connection.request("POST", path, json.dumps(body),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def start_server(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(lambda: self.thread.join(timeout=2))
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def test_demo_scenario_load_reuses_the_existing_review(self):
        self.start_server()
        status, first = self.request("/api/scenario/load", {
            "id": "travel-rebooking", "reuse": True})
        status2, second = self.request("/api/scenario/load", {
            "id": "travel-rebooking", "reuse": True})

        self.assertEqual(200, status)
        self.assertEqual(200, status2)
        self.assertTrue(second["restored"])
        self.assertEqual(first["skill"]["id"], second["skill"]["id"])
        self.assertEqual(1, len(server.STATE["skills"]))

    def test_all_four_counterbalanced_cells_freeze_the_assignment(self):
        self.start_server()
        for scenario in ("travel-rebooking", "expense-review"):
            for condition in ("workspace", "chat"):
                server.STATE = copy.deepcopy(EMPTY_STATE)
                status, payload = self.request("/api/scenario/load", {
                    "id": scenario,
                    "study": {"session": "session-%s-%s" % (scenario, condition),
                              "participant": "P001", "condition": condition,
                              "period": "1", "formal": True},
                })
                self.assertEqual(200, status)
                self.assertEqual(scenario, payload["study_context"]["scenario_id"])
                self.assertEqual(condition, payload["study_context"]["condition"])
                self.assertEqual(payload["work_order"]["task_hash"],
                                 payload["study_context"]["task_hash"])

    def test_launcher_bound_process_rejects_a_different_formal_link(self):
        server.FORMAL_ASSIGNMENT = {
            "participant": "P008", "period": "2", "condition": "chat",
            "scenario_id": "expense-review"}
        self.start_server()
        status, payload = self.request("/api/scenario/load", {
            "id": "travel-rebooking",
            "study": {"session": "wrong", "participant": "P008",
                      "condition": "chat", "period": "2", "formal": True},
        })

        self.assertEqual(409, status)
        self.assertIn("隔离进程", payload["error"])
        self.assertIsNone(server.STATE["active"])

    def test_assignment_sheet_balances_order_domain_and_condition(self):
        rows = study_assignments.assignments(
            ["P001", "P002", "P003", "P004"], 8800)
        self.assertEqual(8, len(rows))
        sequences = {(row["participant"], row["period"]):
                     (row["condition"], row["scenario"]) for row in rows}
        self.assertEqual(("workspace", "travel-rebooking"), sequences[("P001", "1")])
        self.assertEqual(("chat", "travel-rebooking"), sequences[("P002", "1")])
        self.assertEqual(("workspace", "expense-review"), sequences[("P003", "1")])
        self.assertEqual(("chat", "expense-review"), sequences[("P004", "1")])
        for participant in ("P001", "P002", "P003", "P004"):
            pair = [sequences[(participant, str(period))] for period in (1, 2)]
            self.assertEqual({"workspace", "chat"}, {row[0] for row in pair})
            self.assertEqual({"travel-rebooking", "expense-review"},
                             {row[1] for row in pair})

    def test_formal_task_is_atomically_archived_and_restorable(self):
        with tempfile.TemporaryDirectory() as directory:
            server.STUDY_DATA_DIR = Path(directory)
            server.REQUIRE_STUDY_ARCHIVE = True
            pack = runtime.get_pack("expense-review")
            skill = runtime.skill_record(pack)
            skill.update({"id": "k19", "version": 1,
                          "study_context": server.study_context({
                              "session": "s1", "participant": "P019",
                              "condition": "chat", "period": "2", "formal": True,
                          }, pack)})
            server.initialize_skill_record(skill)
            server.STATE["skills"][skill["id"]] = skill
            server.STATE["active"] = skill["id"]
            snapshot = runtime.case_snapshot(pack, pack["entry_case"])
            snapshot.update({"id": "s20", "skill": skill["id"]})
            server.STATE["snapshots"][snapshot["id"]] = snapshot

            checkpoint = server.archive_formal_task(stage="checkpoint")
            path = Path(directory) / checkpoint["file"]
            self.assertTrue(path.exists())
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("checkpoint", document["archive"]["stage"])
            self.assertFalse(list(Path(directory).glob(".*.tmp-*")))

            server.STATE["questionnaires"].append({
                "id": "q21", "skill": skill["id"], "status": "completed"})
            completed = server.archive_formal_task(stage="completed")
            self.assertEqual(path.name, completed["file"])
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("completed", document["archive"]["stage"])
            self.assertEqual("completed", document["questionnaires"][0]["status"])

            server.STATE = copy.deepcopy(EMPTY_STATE)
            restored = server.restore_export_document(document)
            self.assertEqual("k19", restored["id"])
            self.assertEqual("P019", restored["study_context"]["participant"])
            self.assertIn("s20", server.STATE["snapshots"])

    def test_formal_recovery_reuses_chat_and_questionnaire_across_browser_session(self):
        pack = runtime.get_pack("travel-rebooking")
        skill = runtime.skill_record(pack)
        skill.update({"id": "k31", "version": 1,
                      "study_context": server.study_context({
                          "session": "old-browser", "participant": "P031",
                          "condition": "chat", "period": "1", "formal": True,
                      }, pack)})
        server.initialize_skill_record(skill)
        server.STATE["skills"][skill["id"]] = skill
        server.STATE["active"] = skill["id"]
        review = {"id": "v32", "skill": skill["id"], "action": "defer",
                  "condition": "chat", "reason": "暂缓"}
        server.STATE["reviews"].append(review)
        server.STATE["chat"].append({"id": "c33", "skill": skill["id"],
                                     "session": "old-browser", "message": "保留边界",
                                     "reply": "已记录"})
        case = server.prediction_holdout(pack)
        record = {"id": "q34", "skill": skill["id"], "session": "old-browser",
                  "review_id": review["id"], "scenario_id": pack["id"],
                  "case_id": case["id"], "status": "presented",
                  "study_context": copy.deepcopy(skill["study_context"]),
                  "artifact": {"hash": skill["content_hash"], "version": 1,
                               "instructions": copy.deepcopy(skill["instructions"])},
                  "question_schema": copy.deepcopy(
                      case["study_measure"]["prediction_questions"])}
        server.STATE["questionnaires"].append(record)

        bootstrap = server.public_chat_bootstrap("new-browser")
        self.assertEqual("保留边界", bootstrap["messages"][0]["message"])

        class FakeHandler:
            def _body(self):
                return {"session": "new-browser"}

            def _json(self, value, code=200):
                return {"status": code, "body": value}

        response = server.Handler.h_api_study_questionnaire_start(FakeHandler())
        self.assertEqual("q34", response["body"]["questionnaire"]["id"])
        self.assertEqual(1, len(server.STATE["questionnaires"]))

    def test_condition_pages_expose_demo_switch_but_lock_formal_mode(self):
        workspace = (server.HERE / "app.html").read_text(encoding="utf-8")
        chat = (server.HERE / "chat.html").read_text(encoding="utf-8")
        self.assertIn("切换工作流", workspace)
        self.assertIn('id="scenarioButton"', chat)
        self.assertIn("function showScenarioPicker()", chat)
        self.assertIn('$("scenarioButton").hidden=true', chat)
        self.assertIn("reuse:!FORMAL", workspace)
        self.assertIn("reuse:!FORMAL", chat)


if __name__ == "__main__":
    unittest.main()
