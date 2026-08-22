#!/usr/bin/env python3
"""
SkillScope 服务端。

开放 skill 可以使用生成后冻结的快照；研究场景使用可复位的本地工具世界。
两条路径共用范围记录、干预、候选 diff、配对执行和发布评审。

    export DEEPSEEK_API_KEY=sk-...
    python3 server.py                  # http://127.0.0.1:8000
"""

import hashlib
import json
import os
import re
import sys
import time
import threading
import urllib.request
import urllib.parse
import concurrent.futures
import copy
import collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import scenario_runtime as scenario_rt

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = os.environ.get("SKILLSCOPE_MODEL", "deepseek-v4-flash")
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
HERE = Path(__file__).parent
PORT = int(os.environ.get("PORT", "8000"))
AGENT_TEMPERATURE = float(os.environ.get("SKILLSCOPE_AGENT_TEMPERATURE", "0.0"))
REVIEW_TEMPERATURE = float(os.environ.get("SKILLSCOPE_REVIEW_TEMPERATURE", "0.0"))
MAX_TOOL_TURNS = int(os.environ.get("SKILLSCOPE_MAX_TOOL_TURNS", "12"))
BASELINE_RUNS = 3
MATCHED_RUNS = 3
GATHER_RUNS = 5
SOURCE_PROBE_RUNS = 3
SOURCE_PREVIEW_MAX_INSTRUCTIONS = 1
BLOCK_PROBE_RUNS = 3
PARTICIPANT_FACT_LABELS = {
    "selected_flight": "所选航班", "arrival_time": "抵达时间",
    "price": "净增费用", "on_time_for_commitment": "固定日程",
    "confirmation_flow": "确认流程", "booking_completed": "改签状态",
    "receipt_id": "票据", "decision": "审核决定", "amount": "票据金额",
    "required_fields_complete": "票据字段", "restricted_category": "品类政策",
    "manager_approval_requested": "主管审批", "ledger_entry_created": "入账状态",
    "decision_recorded": "决定记录",
}
_STUDY_DATA_DIR = os.environ.get("SKILLSCOPE_DATA_DIR", "").strip()
STUDY_DATA_DIR = Path(_STUDY_DATA_DIR).expanduser() if _STUDY_DATA_DIR else None
REQUIRE_STUDY_ARCHIVE = os.environ.get("SKILLSCOPE_REQUIRE_ARCHIVE", "0") == "1"
RESUME_FILE = os.environ.get("SKILLSCOPE_RESUME_FILE", "").strip()
FORMAL_ASSIGNMENT = {
    "participant": os.environ.get("SKILLSCOPE_PARTICIPANT", "").strip(),
    "period": os.environ.get("SKILLSCOPE_PERIOD", "").strip(),
    "condition": os.environ.get("SKILLSCOPE_CONDITION", "").strip(),
    "scenario_id": os.environ.get("SKILLSCOPE_SCENARIO", "").strip(),
}

LOCK = threading.Lock()
ARCHIVE_LOCK = threading.Lock()
STATE = {"skills": {}, "active": None, "snapshots": {},
         "runs": [], "situations": [], "probes": [], "reviews": [], "events": [],
         "chat": [], "questionnaires": [], "seq": 0}

POST_TASK_ITEMS = [
    {"id": "understand_change", "text": "我能准确说明最终候选相对原版本改变了什么。"},
    {"id": "anticipate_behavior", "text": "我能预测最终候选在相近情况中的处理行为。"},
    {"id": "boundary_awareness", "text": "我清楚哪些适用边界仍未解决或需要人工判断。"},
    {"id": "evidence_sufficient", "text": "现有执行证据足以支持我作出本次发布决定。"},
    {"id": "perceived_control", "text": "我保留了对规则取舍和是否发布的控制权。"},
    {"id": "ease_of_use", "text": "完成这次评审对我来说是容易的。"},
    {"id": "information_revisitable", "text": "作出决定前，我能重新找到重要的规则、情况和执行结果。"},
    {"id": "capability_access", "text": "系统提供了完成评审所需的 Skill、相关情况和执行结果。"},
]

SEMANTIC_EVENT_NAMES = frozenset((
    "task_started", "intent_committed", "scope_committed",
    "candidate_revealed", "comparison_viewed", "decision_submitted",
    "task_completed",
))

RAW_TLX_ITEMS = [
    {"id": "mental_demand", "text": "脑力需求", "left": "很低", "right": "很高"},
    {"id": "physical_demand", "text": "身体需求", "left": "很低", "right": "很高"},
    {"id": "temporal_demand", "text": "时间压力", "left": "很低", "right": "很高"},
    {"id": "performance", "text": "对自己完成任务表现的不满意程度", "left": "很低", "right": "很高"},
    {"id": "effort", "text": "投入努力", "left": "很低", "right": "很高"},
    {"id": "frustration", "text": "挫败感", "left": "很低", "right": "很高"},
]


def cur():
    """当前 skill 记录；不存在时返回 None。"""
    return STATE["skills"].get(STATE["active"])


def mine(seq):
    """只保留属于当前 skill 的记录。"""
    a = STATE["active"]
    return [x for x in seq if x.get("skill") == a]


def nid(p):
    with LOCK:
        STATE["seq"] += 1
        return "%s%d" % (p, STATE["seq"])


def record_semantic_event(name, sk=None, data=None, session="", source="server"):
    """Persist one condition-neutral study milestone, deduplicated by artifact state.

    UI-specific clicks and chat turns remain available for debugging, but the
    analysis path relies on these shared milestones only.
    """
    if name not in SEMANTIC_EVENT_NAMES:
        raise ValueError("unknown semantic event: %s" % name)
    sk = sk or cur()
    if not sk:
        return None
    payload = copy.deepcopy(data or {})
    study = sk.get("study_context") or {}
    condition = study.get("condition") or payload.get("condition") or "unspecified"
    identity = {
        "name": name,
        "skill": sk.get("id"),
        "review_round": sk.get("review_round") or 1,
    }
    if name == "task_started":
        identity["task"] = study.get("task_hash") or (sk.get("work_order") or {}).get("task_hash")
    elif name == "intent_committed":
        identity["case_id"] = payload.get("case_id")
        identity["scope_version"] = payload.get("scope_version")
    elif name == "scope_committed":
        identity["scope_hash"] = payload.get("scope_hash")
    elif name in ("candidate_revealed", "comparison_viewed"):
        identity["candidate_hash"] = payload.get("candidate_hash") or \
            (sk.get("candidate") or {}).get("content_hash")
    elif name in ("decision_submitted", "task_completed"):
        identity["review_id"] = payload.get("review_id")
        identity["action"] = payload.get("action")
    semantic_key = full_hash(identity)
    event_id = nid("e")
    with LOCK:
        existing = next((row for row in STATE.setdefault("events", [])
                         if row.get("semantic_key") == semantic_key), None)
        if existing:
            return copy.deepcopy(existing)
        rec = {
            "id": event_id, "skill": sk.get("id"), "session": session[:80],
            "name": name, "data": payload, "condition": condition,
            "semantic": True, "semantic_key": semantic_key,
            "source": source, "at": time.time(),
        }
        STATE["events"].append(rec)
    return copy.deepcopy(rec)


def shash(o):
    return hashlib.sha256(json.dumps(o, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()[:12]


def full_hash(o):
    return hashlib.sha256(json.dumps(o, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def max_imported_sequence(value):
    """Recover the monotonic id counter when restoring an exported in-memory review."""
    best = 0
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "id" and isinstance(child, str):
                match = re.fullmatch(r"[cekmprstqv](\d+)", child)
                if match:
                    best = max(best, int(match.group(1)))
            best = max(best, max_imported_sequence(child))
    elif isinstance(value, list):
        for child in value:
            best = max(best, max_imported_sequence(child))
    return best


def intervention_provenance(sk, snap, variant, instructions, baseline_runs,
                            intervention_runs, evidence_role):
    """Reconstruction fields shared by source and candidate-block probes."""
    baseline_artifact = sk.get("candidate") if evidence_role == "candidate_block" else sk
    return {
        "evidence_role": evidence_role,
        "baseline_artifact_hash": (baseline_artifact or {}).get("content_hash"),
        "intervention_artifact_hash": full_hash(instructions),
        "intervention": copy.deepcopy(variant),
        "baseline_run_ids": [row["id"] for row in baseline_runs],
        "intervention_run_ids": [row["id"] for row in intervention_runs],
        "held_constant": {
            "case_hash": snap.get("case_hash"),
            "world_hash": snap.get("world_hash"),
            "tool_schema_hash": snap.get("tool_schema_hash"),
            "model": MODEL,
            "review_temperature": REVIEW_TEMPERATURE,
            "agent_temperature": AGENT_TEMPERATURE,
        },
    }


def initialize_skill_record(sk):
    """Attach reconstruction fields shared by parsed and packaged skills."""
    # Exports created before the participant work-order shell was introduced
    # still need to render in the current product.  Rehydrate only the public
    # brief from the versioned scenario pack; formal assignment metadata must
    # remain absent because it cannot be reconstructed truthfully.
    if sk.get("scenario_id") and not sk.get("work_order"):
        try:
            sk["work_order"] = scenario_rt.public_work_order(
                scenario_rt.get_pack(sk["scenario_id"]))
        except KeyError:
            pass
    sk.setdefault("study_context", {})
    sk["hash"] = shash(sk["instructions"])
    sk["content_hash"] = full_hash(sk["instructions"])
    sk.setdefault("versions", [])
    sk.setdefault("candidate", None)
    sk.setdefault("scope_version", 1)
    sk.setdefault("scope_history", [{
        "version": 1, "parent": None, "created_at": time.time(), "items": [],
        "hash": full_hash([]),
    }])
    sk.setdefault("manifests", [])
    sk.setdefault("regression_cases", [])
    sk.setdefault("scope_plans", [])
    sk.setdefault("active_scope_plan_id", None)
    sk.setdefault("repair_previews", [])
    sk.setdefault("active_repair_preview_id", None)
    sk.setdefault("review_round", 1)
    sk.setdefault("candidate_rounds", [])
    sk.setdefault("scope_revision_required", False)
    sk.setdefault("first_candidate_revealed_at", None)
    sk.setdefault("last_compare_budget", None)
    return sk


def export_document(skill_id=None):
    """Return one self-contained task record without relying on active-skill helpers."""
    skill_id = skill_id or STATE.get("active")
    with LOCK:
        sk = STATE.get("skills", {}).get(skill_id)
        if not sk:
            return None
        return {
            "format": "skillscope/2", "exported": time.time(), "model": MODEL,
            "review_temperature": REVIEW_TEMPERATURE,
            "agent_temperature": AGENT_TEMPERATURE,
            "skill": copy.deepcopy(sk),
            "snapshots": copy.deepcopy([
                row for row in STATE.get("snapshots", {}).values()
                if row.get("skill") == skill_id]),
            "runs": copy.deepcopy([row for row in STATE.get("runs", [])
                                   if row.get("skill") == skill_id]),
            "situations": copy.deepcopy([row for row in STATE.get("situations", [])
                                         if row.get("skill") == skill_id]),
            "probes": copy.deepcopy([row for row in STATE.get("probes", [])
                                     if row.get("skill") == skill_id]),
            "reviews": copy.deepcopy([row for row in STATE.get("reviews", [])
                                      if row.get("skill") == skill_id]),
            "events": copy.deepcopy([row for row in STATE.get("events", [])
                                     if row.get("skill") == skill_id]),
            "chat": copy.deepcopy([row for row in STATE.get("chat", [])
                                   if row.get("skill") == skill_id]),
            "questionnaires": copy.deepcopy([
                row for row in STATE.get("questionnaires", [])
                if row.get("skill") == skill_id]),
        }


def _archive_component(value, fallback):
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-.")
    return (value or fallback)[:80]


def study_archive_filename(sk):
    """Stable filename for a participant × condition × period × task assignment."""
    context = sk.get("study_context") or {}
    fields = [
        _archive_component(context.get("participant"), "participant"),
        "p" + _archive_component(context.get("period"), "unknown"),
        _archive_component(context.get("condition"), "condition"),
        _archive_component(sk.get("scenario_id"), "scenario"),
        _archive_component(context.get("task_hash"), "task")[:16],
    ]
    return "__".join(fields) + ".json"


def ensure_study_archive_ready():
    if STUDY_DATA_DIR is None:
        if REQUIRE_STUDY_ARCHIVE:
            raise RuntimeError("正式任务未配置 SKILLSCOPE_DATA_DIR")
        return False
    STUDY_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STUDY_DATA_DIR.is_dir():
        raise RuntimeError("SKILLSCOPE_DATA_DIR 不是目录")
    return True


def archive_formal_task(skill_id=None, stage=None):
    """Atomically checkpoint one formal task; final uses the same recoverable file."""
    skill_id = skill_id or STATE.get("active")
    sk = STATE.get("skills", {}).get(skill_id)
    if not sk or not (sk.get("study_context") or {}).get("formal"):
        return None
    if not ensure_study_archive_ready():
        return None
    with ARCHIVE_LOCK:
        document = export_document(skill_id)
        if not document:
            return None
        completed = any(row.get("status") == "completed"
                        for row in document.get("questionnaires") or [])
        saved_at = time.time()
        document["archive"] = {
            "stage": stage or ("completed" if completed else "checkpoint"),
            "saved_at": saved_at,
            "atomic": True,
        }
        payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        destination = STUDY_DATA_DIR / study_archive_filename(document["skill"])
        temporary = destination.with_name(
            ".%s.tmp-%d-%d" % (destination.name, os.getpid(), threading.get_ident()))
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(destination))
        return {
            "saved": True, "file": destination.name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "saved_at": saved_at, "stage": document["archive"]["stage"],
        }


def restore_export_document(document):
    """Restore an exact single-task checkpoint before the HTTP server starts."""
    sk = copy.deepcopy((document or {}).get("skill"))
    if not sk or not sk.get("id") or "instructions" not in sk:
        raise ValueError("恢复文件不是有效的 SkillScope 任务记录")
    if not (sk.get("study_context") or {}).get("formal"):
        raise ValueError("只允许自动恢复正式任务记录")
    initialize_skill_record(sk)
    sid = sk["id"]
    restored = {key: copy.deepcopy((document or {}).get(key) or [])
                for key in ("runs", "situations", "probes", "reviews", "events", "chat",
                            "questionnaires")}
    snapshots = copy.deepcopy((document or {}).get("snapshots") or [])
    with LOCK:
        STATE["skills"].clear()
        STATE["skills"][sid] = sk
        STATE["active"] = sid
        STATE["snapshots"] = {row["id"]: row for row in snapshots}
        for key, rows in restored.items():
            STATE[key] = rows
        STATE["seq"] = max_imported_sequence(document)
    return sk


def load_resume_file():
    if not RESUME_FILE:
        return None
    path = Path(RESUME_FILE).expanduser()
    document = json.loads(path.read_text(encoding="utf-8"))
    resume_skill = (document or {}).get("skill") or {}
    try:
        resume_pack = scenario_rt.get_pack(resume_skill.get("scenario_id"))
    except KeyError as error:
        raise ValueError("恢复文件中的场景无效") from error
    problem = configured_assignment_problem(
        resume_skill.get("study_context") or {}, resume_pack)
    if problem:
        raise ValueError("恢复文件与启动参数不一致：%s" % problem)
    sk = restore_export_document(document)
    print("已恢复正式任务检查点 %s" % path, file=sys.stderr)
    return sk


def scope_items(skill_id=None, include_superseded=False):
    skill_id = skill_id or STATE["active"]
    return [copy.deepcopy(row) for row in STATE["situations"]
            if row.get("skill") == skill_id and
            (include_superseded or not row.get("superseded_at"))]


def current_scope_plan(sk):
    """Return the active, versioned intent-to-case plan for one repair round."""
    if not sk:
        return None
    plan_id = sk.get("active_scope_plan_id")
    return next((copy.deepcopy(row) for row in reversed(sk.get("scope_plans") or [])
                 if row.get("id") == plan_id), None)


def _active_incident_commitment(sk):
    if not sk or not sk.get("scenario_id"):
        return ""
    entry_case = scenario_rt.get_pack(sk["scenario_id"])["entry_case"]
    rows = [row for row in scope_items(sk["id"])
            if (row.get("case_id") or
                (STATE["snapshots"].get(row.get("sid")) or {}).get("case_id")) == entry_case]
    return (rows[-1].get("commitment") or "").strip() if rows else ""


def _fallback_scope_plan(commitment, candidates):
    """Keep the review usable and traceable when intent planning is unavailable."""
    return {
        "intent": {
            "summary": "本次修复目标：%s" % commitment,
            "trigger": "以负责人刚刚说明的适用条件为准",
            "required_action": "以负责人刚刚说明的目标处理为准",
            "forbidden_action": "不得违反负责人明确提出的限制",
            "ambiguities": [],
        },
        "cases": [{
            "case_id": row["case_id"],
            "relation_type": row.get("relation_type") or "related-case",
            "why_relevant": row.get("intent_link") or row.get("why") or "检查相邻行为",
            "owner_question": ((row.get("review_prompt") or {}).get("boundary_question") or ""),
        } for row in candidates],
        "source": "frozen-case-bank-fallback",
    }


def ensure_scope_plan(sk, commitment=None):
    """Ground an owner-authored change in a reproducible set of executable cases.

    The model may rank and explain only prevalidated case ids. It cannot create a
    tool world, read an oracle, or modify a case. Formal tasks retain the full
    calibrated case set so both conditions receive identical evidence.
    """
    if not sk or not sk.get("scenario_id"):
        return None
    pack = scenario_rt.get_pack(sk["scenario_id"])
    entry_id = pack["entry_case"]
    commitment = (commitment or _active_incident_commitment(sk)).strip()
    if not commitment:
        return None
    plan_key = full_hash({
        "pack": pack.get("pack_hash"), "entry_case": entry_id,
        "commitment": commitment,
    })
    active = current_scope_plan(sk)
    if active and active.get("plan_key") == plan_key:
        return active

    candidates = scenario_rt.neighbouring_cases(pack, entry_id)
    public_candidates = [{
        "case_id": row["case_id"], "summary": row.get("text"),
        "suggested_role": row.get("suggest"),
        "relation_type": row.get("relation_type"),
        "design_rationale": row.get("intent_link") or row.get("why"),
        "changed_factors": copy.deepcopy(row.get("changed_factors") or []),
        "open_question": ((row.get("review_prompt") or {}).get("boundary_question") or ""),
    } for row in candidates]
    incident = _incident_snap()
    incident_facts = {}
    if incident:
        runs = _baseline_case_runs(sk, incident)
        if runs:
            incident_facts = copy.deepcopy(runs[-1].get("facts") or {})
    public_tools = [{"label": row.get("label"), "description": row.get("description"),
                     "kind": row.get("kind")}
                    for row in sk.get("tools") or []]
    skill_text = "\n".join("%s. %s" % (row.get("n"), row.get("text"))
                           for row in sk.get("instructions") or [])
    planned = ask(P_SCOPE_PLAN, "SCENARIO: %s\nOWNER CHANGE: %s\nCURRENT SKILL:\n%s\n"
                  "AVAILABLE PRODUCT ACTIONS: %s\nINCIDENT OBSERVABLE FACTS: %s\n"
                  "CANDIDATE CASES: %s" % (
                      pack.get("title"), commitment,
                      skill_text,
                      json.dumps(public_tools, ensure_ascii=False),
                      json.dumps(incident_facts, ensure_ascii=False),
                      json.dumps(public_candidates, ensure_ascii=False)), 2600) or {}
    if planned.get("error") or not isinstance(planned.get("cases"), list):
        normalized = _fallback_scope_plan(commitment, candidates)
    else:
        normalized = {
            "intent": copy.deepcopy(planned.get("intent") or {}),
            "cases": copy.deepcopy(planned.get("cases") or []),
            "source": "intent-conditioned-frozen-case-bank",
        }

    intent = normalized.get("intent") or {}
    summary = (intent.get("summary") or "").strip()
    intent = {
        "summary": summary or "本次修复目标：%s" % commitment,
        "trigger": (intent.get("trigger") or "").strip(),
        "required_action": (intent.get("required_action") or "").strip(),
        "forbidden_action": (intent.get("forbidden_action") or "").strip(),
        "ambiguities": [str(value).strip() for value in
                        (intent.get("ambiguities") or []) if str(value).strip()][:4],
    }
    available_labels = [str(row.get("label") or "") for row in sk.get("tools") or []]
    if "管理员" in commitment and any("主管审批" in label for label in available_labels):
        mapping_note = "你说的“管理员审核”在当前系统中可执行的动作是“主管审批”；若两者不是同一角色，需要进一步确认。"
        if mapping_note not in intent["ambiguities"]:
            intent["ambiguities"] = (intent["ambiguities"] + [mapping_note])[:4]
    by_id = {row["case_id"]: row for row in candidates}
    relation_types = {"outside-trigger", "existing-safeguard", "definition-boundary",
                      "policy-limit", "action-boundary", "related-case"}
    selected = []
    seen = set()
    for proposal in normalized.get("cases") or []:
        case_id = proposal.get("case_id")
        if case_id not in by_id or case_id in seen:
            continue
        base = copy.deepcopy(by_id[case_id])
        relation_type = proposal.get("relation_type")
        if relation_type not in relation_types:
            relation_type = base.get("relation_type") or "related-case"
        base["relation_type"] = relation_type
        base["why_relevant"] = ((proposal.get("why_relevant") or "").strip() or
                                base.get("intent_link") or base.get("why") or "")
        base["owner_question"] = ((proposal.get("owner_question") or "").strip() or
                                  (base.get("review_prompt") or {}).get("boundary_question") or "")
        selected.append(base)
        seen.add(case_id)

    # A product-like demo may omit a weakly related third case. The formal study
    # preserves the calibrated four-case evidence contract in both conditions.
    minimum = min(2, len(candidates))
    for base in candidates:
        if len(selected) >= minimum:
            break
        if base["case_id"] not in seen:
            copy_base = copy.deepcopy(base)
            copy_base["why_relevant"] = copy_base.get("intent_link") or copy_base.get("why")
            copy_base["owner_question"] = ((copy_base.get("review_prompt") or {}).get(
                "boundary_question") or "")
            selected.append(copy_base)
            seen.add(base["case_id"])
    if (sk.get("study_context") or {}).get("formal"):
        for base in candidates:
            if base["case_id"] in seen:
                continue
            copy_base = copy.deepcopy(base)
            copy_base["why_relevant"] = copy_base.get("intent_link") or copy_base.get("why")
            copy_base["owner_question"] = ((copy_base.get("review_prompt") or {}).get(
                "boundary_question") or "")
            selected.append(copy_base)
            seen.add(base["case_id"])
    selected = selected[:3]

    plan = {
        "id": nid("q"), "plan_key": plan_key,
        "scenario_id": sk["scenario_id"], "entry_case": entry_id,
        "owner_commitment": commitment, "intent": intent,
        "cases": selected,
        "excluded_case_ids": [row["case_id"] for row in candidates
                              if row["case_id"] not in seen],
        "source": normalized.get("source"), "model": MODEL,
        "review_temperature": REVIEW_TEMPERATURE,
        "planner_prompt_version": 2,
        "incident_facts_hash": full_hash(incident_facts),
        "usage_tokens": planned.get("_usage", 0) if isinstance(planned, dict) else 0,
        "created_at": time.time(),
    }
    plan["hash"] = full_hash({key: value for key, value in plan.items()
                              if key not in ("id", "created_at", "usage_tokens")})

    superseded = False
    with LOCK:
        previous_id = sk.get("active_scope_plan_id")
        if previous_id and previous_id != plan["id"]:
            for row in STATE["situations"]:
                if row.get("skill") != sk["id"] or row.get("superseded_at"):
                    continue
                case_id = row.get("case_id") or \
                    (STATE["snapshots"].get(row.get("sid")) or {}).get("case_id")
                if case_id and case_id != entry_id:
                    row["superseded_at"] = time.time()
                    row["superseded_by_plan"] = plan["id"]
                    superseded = True
        sk.setdefault("scope_plans", []).append(copy.deepcopy(plan))
        sk["active_scope_plan_id"] = plan["id"]
        if superseded:
            sk["scope_version"] = (sk.get("scope_version") or 1) + 1
    if superseded:
        freeze_scope_version(sk)
    return copy.deepcopy(plan)


def planned_related_cases(sk, commitment=None):
    plan = ensure_scope_plan(sk, commitment)
    return copy.deepcopy((plan or {}).get("cases") or [])


def current_repair_preview(sk):
    """Return the active source-location preview for the current review round."""
    if not sk:
        return None
    preview_id = sk.get("active_repair_preview_id")
    return next((copy.deepcopy(row) for row in reversed(sk.get("repair_previews") or [])
                 if row.get("id") == preview_id), None)


def _source_preview_fallback(sk, pack, incident_id, commitment_ids):
    defaults = pack.get("source_preview_defaults") or []
    allowed_numbers = {row.get("n") for row in sk.get("instructions") or []}
    for row in defaults:
        if row.get("instruction") in allowed_numbers:
            return {
                "instruction": row["instruction"],
                "case_id": row.get("case_id") or incident_id,
                "question": row.get("question") or
                            "临时改变这条原指令时，当前问题中的可观测处理是否变化？",
                "commitment_ids": list(commitment_ids)[:3],
                "inverted_text": (row.get("inverted_text") or "").strip(),
                "planner_source": "scenario-calibrated-fallback",
            }
    # Open-skill fallback remains bounded and explicit. It is only a location
    # cue; selecting the first instruction never becomes normative evidence.
    first = next(iter(sk.get("instructions") or []), None)
    return ({"instruction": first.get("n"), "case_id": incident_id,
             "question": "临时改变这条原指令时，当前问题中的可观测处理是否变化？",
             "commitment_ids": list(commitment_ids)[:3],
             "planner_source": "bounded-structural-fallback"} if first else None)


def ensure_repair_preview(sk):
    """Run one frozen, reconstructable M0 source-location check before drafting.

    The planner can select only an existing instruction and an already executed
    product case. The automatic budget is fixed: delete and minimal inversion,
    three executions each, for one source instruction. Results locate a region
    of the original skill and never validate candidate text.
    """
    if not sk:
        return None
    readiness = scope_readiness(sk)
    if not readiness.get("ready"):
        raise ValueError("请先完成适用范围确认")
    situations = scope_items(sk["id"])
    normative = [row for row in situations if row.get("disposition") != "excluded"]
    if not normative:
        raise ValueError("至少需要一项可执行的行为承诺")
    scope_hash = (sk.get("scope_history") or [{}])[-1].get("hash")
    preview_key = full_hash({
        "skill": sk.get("content_hash"), "scope": scope_hash,
        "plan": (current_scope_plan(sk) or {}).get("hash"),
        "round": sk.get("review_round") or 1,
    })
    active = current_repair_preview(sk)
    if active and active.get("preview_key") == preview_key:
        return active

    pack = scenario_rt.get_pack(sk["scenario_id"]) if sk.get("scenario_id") else None
    incident_id = pack.get("entry_case") if pack else \
        (STATE["snapshots"].get(normative[0].get("sid")) or {}).get("case_id")
    allowed_cases = {}
    public_commitments = []
    for row in normative:
        snap = STATE["snapshots"].get(row.get("sid")) or {}
        case_id = row.get("case_id") or snap.get("case_id")
        if case_id:
            allowed_cases[case_id] = {
                "case_id": case_id, "summary": snap.get("summary") or row.get("case_context"),
                "disposition": row.get("disposition"),
            }
        public_commitments.append({
            "id": row.get("id"), "case_id": case_id,
            "disposition": row.get("disposition"), "text": row.get("commitment"),
        })
    instructions = [{"n": row.get("n"), "text": row.get("text")}
                    for row in sk.get("instructions") or []]
    formal = bool((sk.get("study_context") or {}).get("formal"))
    if formal and pack:
        planned = {}
        proposal = _source_preview_fallback(
            sk, pack, incident_id, [row.get("id") for row in normative]) or {}
        proposal["planner_source"] = "scenario-calibrated-protocol"
    else:
        planned = ask(P_SOURCE_PLAN,
                      "INSTRUCTIONS:\n%s\n\nCONFIRMED COMMITMENTS:\n%s\n\nALLOWED CASES:\n%s" % (
                          json.dumps(instructions, ensure_ascii=False),
                          json.dumps(public_commitments, ensure_ascii=False),
                          json.dumps(list(allowed_cases.values()), ensure_ascii=False)), 1600) or {}
        proposal = copy.deepcopy(planned.get("probe") or {})
    allowed_numbers = {row.get("n") for row in instructions}
    commitment_ids = {row.get("id") for row in normative}
    try:
        proposal["instruction"] = int(proposal.get("instruction"))
    except (TypeError, ValueError):
        proposal["instruction"] = None
    if proposal.get("instruction") not in allowed_numbers or \
            proposal.get("case_id") not in allowed_cases:
        proposal = _source_preview_fallback(
            sk, pack or {}, incident_id, [row.get("id") for row in normative]) or {}
    proposal["commitment_ids"] = [value for value in proposal.get("commitment_ids") or []
                                  if value in commitment_ids][:3] or \
                                 [row.get("id") for row in normative[:1]]
    if not proposal.get("instruction") or proposal.get("case_id") not in allowed_cases:
        raise ValueError("无法为当前范围定位可执行的原指令检查")

    snap = _snapshot_for_case(proposal["case_id"])
    if not snap:
        raise ValueError("来源证据所需的情况尚未执行")
    baseline = _baseline_case_runs(sk, snap)
    if not baseline:
        baseline = execute_batch(sk["instructions"], snap, {}, BASELINE_RUNS)
    preview_id = nid("e")
    number = proposal["instruction"]
    source = next(row for row in sk["instructions"] if row.get("n") == number)
    evidence_ids = []
    evidence_rows = []
    variants = [("delete", {"mask": [number]})]
    if proposal.get("inverted_text"):
        inverted_text = proposal["inverted_text"].strip()
    else:
        inverted = ask(P_INVERT, source.get("text") or "", 900) or {}
        inverted_text = (inverted.get("text") or "").strip()
    if not inverted_text or inverted_text == (source.get("text") or "").strip():
        raise ValueError("系统无法形成有效的规则对照，请重新分析建议的修改位置")
    variants.append(("invert", {"rewrite": {"n": number, "text": inverted_text}}))
    for kind, variant in variants:
        executed_instructions, _ = instructions_for(variant)
        runs = execute_batch(executed_instructions, snap, variant, SOURCE_PROBE_RUNS)
        keys = discriminating(baseline, runs)
        changed, changed_fields, weak = compare_fields(baseline, runs, keys)
        rec = {
            "id": nid("p"), "skill": sk["id"], "kind": kind, "n": number,
            "note": ("临时移除原指令 %d" % number if kind == "delete" else
                     "将原指令 %d 最小反转为：%s" % (number, inverted_text)),
            "sid": snap["id"], "case_id": snap.get("case_id"),
            "case_summary": snap.get("summary"), "changed": changed,
            "changed_fields": changed_fields, "confidence": "unstable" if weak else "ok",
            "k": len(runs), "question": proposal.get("question") or "",
            "commitment_ids": copy.deepcopy(proposal["commitment_ids"]),
            "preview_id": preview_id, "automatic": True,
        }
        rec.update(intervention_provenance(
            sk, snap, variant, executed_instructions, baseline, runs, "source_location"))
        with LOCK:
            STATE["probes"].append(rec)
        evidence_ids.append(rec["id"])
        evidence_rows.append({
            "id": rec["id"], "kind": kind, "changed": changed,
            "changed_fields": changed_fields, "confidence": rec["confidence"],
        })

    preview = {
        "id": preview_id, "preview_key": preview_key,
        "scope_version": sk.get("scope_version") or 1, "scope_hash": scope_hash,
        "scope_plan_id": (current_scope_plan(sk) or {}).get("id"),
        "review_round": sk.get("review_round") or 1,
        "instruction": number, "instruction_text": source.get("text"),
        "case_id": snap.get("case_id"), "case_summary": snap.get("summary"),
        "question": proposal.get("question") or "",
        "commitment_ids": copy.deepcopy(proposal["commitment_ids"]),
        "evidence_ids": evidence_ids, "evidence": evidence_rows,
        "planner_source": proposal.get("planner_source") or "model-bounded-selection",
        "planner_model": (MODEL if proposal.get("planner_source") ==
                          "model-bounded-selection" or not proposal.get("planner_source") else None),
        "planner_prompt_version": 1,
        "run_budget": {"instructions": SOURCE_PREVIEW_MAX_INSTRUCTIONS,
                       "interventions_per_instruction": len(variants),
                       "runs_per_intervention": SOURCE_PROBE_RUNS},
        "used_at": None, "presented_at": None, "created_at": time.time(),
    }
    preview["hash"] = full_hash({key: value for key, value in preview.items()
                                  if key not in ("id", "created_at", "used_at",
                                                 "presented_at")})
    with LOCK:
        sk.setdefault("repair_previews", []).append(copy.deepcopy(preview))
        sk["active_repair_preview_id"] = preview["id"]
    return copy.deepcopy(preview)


def public_repair_preview(sk, preview=None):
    preview = preview or current_repair_preview(sk)
    if not preview:
        return None
    ids = set(preview.get("evidence_ids") or [])
    evidence = [copy.deepcopy(row) for row in STATE["probes"]
                if row.get("skill") == sk.get("id") and row.get("id") in ids]
    case = None
    participant_question = preview.get("question") or ""
    if sk.get("scenario_id") and preview.get("case_id"):
        try:
            pack = scenario_rt.get_pack(sk["scenario_id"])
            case = scenario_rt.get_case(pack, preview.get("case_id"))
            mechanics = participant_question.lower()
            if ("临时移除" in participant_question or "最小反转" in participant_question or
                    "temporarily removed" in mechanics or "minimally inverted" in mechanics or
                    "deletion" in mechanics or "inversion" in mechanics):
                replacement = next((row.get("question") for row in
                                    pack.get("source_preview_defaults") or []
                                    if row.get("instruction") == preview.get("instruction") and
                                    row.get("case_id") == preview.get("case_id")), None)
                participant_question = replacement or \
                    "这条指令是否影响当前情况中需要关注的处理结果？"
        except KeyError:
            case = None

    def representative_outcome(run_ids):
        wanted = set(run_ids or [])
        rows = [row for row in STATE["runs"]
                if row.get("skill") == sk.get("id") and row.get("id") in wanted
                and not row.get("error")]
        if not rows:
            return ""
        summary = summarize(rows)
        group = (summary.get("groups") or [{}])[0]
        return _compact_case_outcome(
            sk, group.get("facts") or {}, group.get("outcome") or "", case)

    changed_labels = []
    for row in evidence:
        labels = [PARTICIPANT_FACT_LABELS.get(key, key.replace("_", " "))
                  for key in row.get("changed_fields") or []]
        row["changed_field_labels"] = labels
        row["baseline_outcome"] = representative_outcome(row.get("baseline_run_ids"))
        row["intervention_outcome"] = representative_outcome(
            row.get("intervention_run_ids"))
        for label in labels:
            if label not in changed_labels:
                changed_labels.append(label)

    stable_changed = any(row.get("changed") is True and
                         row.get("confidence") == "ok" for row in evidence)
    any_changed = any(row.get("changed") is True for row in evidence)
    if stable_changed:
        assessment = {
            "status": "related", "label": "与当前问题相关",
            "summary": ("在相同情况和工具数据下，改变这条规则后，Agent 的处理结果发生了"
                        "稳定变化%s。" %
                        ("，变化集中在%s" % "、".join(changed_labels)
                         if changed_labels else "")),
        }
    elif any_changed:
        assessment = {
            "status": "uncertain", "label": "关联仍需确认",
            "summary": "改变这条规则后观察到了结果差异，但重复执行的一致性有限。",
        }
    else:
        assessment = {
            "status": "not_confirmed", "label": "尚未确认直接关联",
            "summary": "在当前情况和工具数据下，改变这条规则后没有观察到稳定的处理结果变化。",
        }
    return {
        "id": preview.get("id"), "hash": preview.get("hash"),
        "scope_version": preview.get("scope_version"),
        "review_round": preview.get("review_round"),
        "instruction": preview.get("instruction"),
        "instruction_text": preview.get("instruction_text"),
        "case_id": preview.get("case_id"), "case_summary": preview.get("case_summary"),
        "question": participant_question, "commitment_ids": preview.get("commitment_ids"),
        "evidence": evidence, "run_budget": copy.deepcopy(preview.get("run_budget") or {}),
        "assessment": assessment,
        "used_at": preview.get("used_at") or preview.get("confirmed_at"),
        "presented_at": preview.get("presented_at"),
        "limitation": ("这一步只帮助选择修改起点。候选是否有效，将在下一步对完整候选的"
                       "全部情况检查中判断。"),
    }


def confirm_repair_preview(sk, source):
    """Record that the non-blocking location cue informed a candidate.

    ``confirmed_at`` from older exports is still read, but new sessions never
    ask the owner to approve this diagnostic cue as policy.
    """
    preview_id = sk.get("active_repair_preview_id") if sk else None
    with LOCK:
        preview = next((row for row in reversed((sk or {}).get("repair_previews") or [])
                        if row.get("id") == preview_id), None)
        if preview and not preview.get("used_at"):
            preview["used_at"] = time.time()
            preview["usage_source"] = source
        return copy.deepcopy(preview) if preview else None


def mark_repair_preview_presented(sk):
    preview_id = sk.get("active_repair_preview_id") if sk else None
    with LOCK:
        preview = next((row for row in reversed((sk or {}).get("repair_previews") or [])
                        if row.get("id") == preview_id), None)
        if preview and not preview.get("presented_at"):
            preview["presented_at"] = time.time()
        return copy.deepcopy(preview) if preview else None


def freeze_scope_version(sk):
    """Store an immutable materialized view of the scope after an edit."""
    items = scope_items(sk["id"])
    record = {
        "version": sk["scope_version"],
        "parent": sk["scope_version"] - 1,
        "created_at": time.time(),
        "items": items,
    }
    record["hash"] = full_hash({"version": record["version"], "items": items})
    sk.setdefault("scope_history", []).append(record)
    return record


def compile_manifest(sk, condition="workspace", selected_probe_ids=None):
    situations = scope_items(sk["id"])
    scope_plan = current_scope_plan(sk)
    repair_preview = current_repair_preview(sk)
    preview_evidence_ids = set((repair_preview or {}).get("evidence_ids") or [])
    probes = [copy.deepcopy(row) for row in STATE["probes"]
              if row.get("skill") == sk["id"] and row.get("n") and
              row.get("id") in preview_evidence_ids]
    if selected_probe_ids is not None:
        selected_probe_ids = set(selected_probe_ids)
        probes = [row for row in probes if row.get("id") in selected_probe_ids]
    visible, withheld, excluded = [], [], []
    for row in situations:
        snap = STATE["snapshots"].get(row.get("sid")) or {}
        item = {
            "id": row["id"],
            "disposition": row["disposition"],
            "commitment": row["commitment"],
            "criterion": copy.deepcopy(row.get("criterion")),
            "case_id": snap.get("case_id"),
            "case_hash": snap.get("case_hash"),
            "task": snap.get("task", ""),
            "judged_at": row.get("judged_at") or row.get("created"),
            "pre_reveal": bool(row.get("pre_reveal")),
            "review_round": row.get("review_round") or 1,
        }
        if row.get("disposition") == "excluded":
            excluded.append(item)
        else:
            (withheld if row.get("sealed") else visible).append(item)
    manifest = {
        "id": nid("m"),
        "scope_version": sk.get("scope_version") or 1,
        "scope_hash": (sk.get("scope_history") or [{}])[-1].get("hash"),
        "scope_plan_id": (scope_plan or {}).get("id"),
        "scope_plan_hash": (scope_plan or {}).get("hash"),
        "repair_preview_id": (repair_preview or {}).get("id"),
        "repair_preview_hash": (repair_preview or {}).get("hash"),
        "review_round": sk.get("review_round") or 1,
        "skill_hash": sk["hash"],
        "skill_content_hash": sk.get("content_hash"),
        "condition": condition,
        "created_at": time.time(),
        "confirmed_at": None,
        "visible_commitments": visible,
        "withheld_commitments": withheld,
        "excluded_cases": excluded,
        "source_evidence": [{
            "id": row.get("id"), "instruction": row.get("n"), "kind": row.get("kind"),
            "changed": row.get("changed"), "snapshot": row.get("sid"),
            "confidence": row.get("confidence"), "note": row.get("note"),
            "changed_fields": copy.deepcopy(row.get("changed_fields") or []),
            "case_id": row.get("case_id"), "case_summary": row.get("case_summary"),
            "question": row.get("question"),
            "commitment_ids": copy.deepcopy(row.get("commitment_ids") or []),
            "preview_id": row.get("preview_id"),
            "evidence_role": row.get("evidence_role") or "source_location",
            "baseline_artifact_hash": row.get("baseline_artifact_hash"),
            "intervention_artifact_hash": row.get("intervention_artifact_hash"),
            "intervention": copy.deepcopy(row.get("intervention")),
            "baseline_run_ids": copy.deepcopy(row.get("baseline_run_ids") or []),
            "intervention_run_ids": copy.deepcopy(row.get("intervention_run_ids") or []),
            "held_constant": copy.deepcopy(row.get("held_constant") or {}),
        } for row in probes],
        "model": MODEL,
        "review_temperature": REVIEW_TEMPERATURE,
        "agent_temperature": AGENT_TEMPERATURE,
    }
    manifest["hash"] = full_hash({k: v for k, v in manifest.items()
                                  if k not in ("id", "created_at", "confirmed_at")})
    with LOCK:
        sk.setdefault("manifests", []).append(manifest)
    return manifest


def scope_readiness(sk):
    """Require a pre-reveal judgment for every in-product scenario case.

    The required disposition is deliberately not enforced: the owner may
    classify a related case differently from the scenario author's suggestion.
    """
    if not sk or not sk.get("scenario_id"):
        return {"ready": bool(scope_items((sk or {}).get("id"))), "missing": []}
    pack = scenario_rt.get_pack(sk["scenario_id"])
    entry = scenario_rt.get_case(pack, pack["entry_case"])
    plan = current_scope_plan(sk)
    planned_ids = [row.get("case_id") for row in (plan or {}).get("cases") or []
                   if row.get("case_id")]
    required_ids = [entry["id"]] + (planned_ids or
                    [row["case_id"] for row in entry.get("neighbours", [])])
    recorded_rows = collections.defaultdict(list)
    active_round = sk.get("review_round") or 1
    for row in scope_items(sk["id"]):
        row_round = row.get("review_round") or 1
        before_round_candidate = row.get("judged_before_candidate_in_round")
        if before_round_candidate is None:
            before_round_candidate = bool(row.get("pre_reveal"))
        if row_round != active_round or not before_round_candidate:
            continue
        case_id = row.get("case_id") or \
            (STATE["snapshots"].get(row.get("sid")) or {}).get("case_id")
        if case_id:
            recorded_rows[case_id].append(row)
    recorded = {case_id for case_id, rows in recorded_rows.items() if len(rows) == 1}
    duplicates = {case_id for case_id, rows in recorded_rows.items() if len(rows) > 1}
    missing = []
    for case_id in required_ids:
        if case_id in recorded:
            continue
        case = scenario_rt.get_case(pack, case_id)
        missing.append({"case_id": case_id, "summary": case.get("summary", case_id),
                        "role": case.get("role"),
                        "reason": "conflicting-duplicates" if case_id in duplicates else "absent"})
    return {"ready": not missing, "missing": missing,
            "required_case_ids": required_ids, "recorded_case_ids": sorted(recorded),
            "duplicate_case_ids": sorted(duplicates), "review_round": active_round}


def pending_chat_manifest(sk):
    for manifest in reversed((sk or {}).get("manifests") or []):
        if manifest.get("condition") != "chat" or manifest.get("confirmed_at"):
            continue
        if manifest.get("scope_version") == (sk.get("scope_version") or 1) and \
                manifest.get("skill_hash") == sk.get("hash"):
            return manifest
    return None


def latest_task_end_review(skill_id=None):
    skill_id = skill_id or STATE["active"]
    rows = [row for row in STATE.get("reviews", [])
            if row.get("skill") == skill_id and row.get("action") in ("publish", "defer")]
    return rows[-1] if rows else None


def prediction_holdouts(pack):
    """All participant-visible-after-task holdouts, in frozen pack order."""
    return [case for case in pack.get("cases", [])
            if case.get("role") == "prediction-holdout"]


def prediction_holdout(pack):
    """Compatibility helper for older exports and operations tests."""
    return next(iter(prediction_holdouts(pack)), None)


def research_holdout(pack):
    return next((case for case in pack.get("cases", [])
                 if case.get("role") == "research-holdout"), None)


def study_context(payload, pack):
    """Normalize assignment metadata supplied by the shared study shell."""
    raw = payload if isinstance(payload, dict) else {}
    condition = (raw.get("condition") or "unspecified").strip().lower()
    if condition not in ("workspace", "chat"):
        condition = "unspecified"
    order = scenario_rt.public_work_order(pack)
    started = time.time()
    return {
        "session": str(raw.get("session") or "")[:80],
        "participant": str(raw.get("participant") or "")[:80],
        "condition": condition,
        "period": str(raw.get("period") or "")[:24],
        "formal": raw.get("formal") is True,
        "scenario_id": pack["id"],
        "task_id": order["id"],
        "task_hash": order["task_hash"],
        "brief_acknowledged_at": started,
        "started_at": started,
    }


def configured_assignment_problem(payload, pack):
    """Reject a formal URL that does not belong to the launcher-bound process."""
    configured = {key: value for key, value in FORMAL_ASSIGNMENT.items() if value}
    if not configured:
        return None
    if len(configured) != len(FORMAL_ASSIGNMENT):
        return "服务进程的正式任务配置不完整"
    raw = payload if isinstance(payload, dict) else {}
    actual = {
        "participant": str(raw.get("participant") or "").strip(),
        "period": str(raw.get("period") or "").strip(),
        "condition": str(raw.get("condition") or "").strip(),
        "scenario_id": pack.get("id"),
    }
    if actual != FORMAL_ASSIGNMENT:
        return "该链接与此隔离进程分配的参与者、条件、轮次或场景不一致"
    return None


def public_questionnaire(record, cases):
    if isinstance(cases, dict):
        cases = [cases]
    cases = list(cases or [])
    try:
        order = scenario_rt.public_work_order(
            scenario_rt.get_pack(record.get("scenario_id")))
    except KeyError:
        order = None
    return {
        "id": record["id"],
        "scenario": record["scenario_id"],
        "work_order": order,
        "study_context": copy.deepcopy(record.get("study_context") or {}),
        "prediction_cases": [{
            "id": case.get("id"),
            "task": {"summary": case.get("summary"), "description": case.get("task")},
            "case_brief": copy.deepcopy((case.get("study_measure") or {}).get("brief") or []),
            "prediction_questions": [{
                "id": row["id"], "prompt": row["prompt"],
                "options": copy.deepcopy(row.get("options") or []),
            } for row in (case.get("study_measure") or {}).get("prediction_questions") or []],
        } for case in cases],
        # Legacy fields keep a previously presented one-case questionnaire renderable.
        "task": ({"summary": cases[0].get("summary"),
                  "description": cases[0].get("task")} if cases else {}),
        "case_brief": (copy.deepcopy((cases[0].get("study_measure") or {}).get("brief") or [])
                       if cases else []),
        "prediction_questions": [{
            "id": row["id"], "prompt": row["prompt"],
            "options": copy.deepcopy(row.get("options") or []),
        } for case in cases
          for row in (case.get("study_measure") or {}).get("prediction_questions") or []],
        "rating_items": copy.deepcopy(POST_TASK_ITEMS),
        "workload_items": copy.deepcopy(RAW_TLX_ITEMS),
        "status": record.get("status"),
    }


def public_chat_bootstrap(session=""):
    """Build a lightweight, execution-grounded opening context for chat mode."""
    sk = cur()
    if not sk:
        return {"active": False, "messages": []}
    snapshots = [row for row in STATE["snapshots"].values()
                 if row.get("skill") == sk["id"] and
                 row.get("case_role") not in scenario_rt.PARTICIPANT_HIDDEN_ROLES]
    incident = next((row for row in snapshots if row.get("case_role") == "incident"),
                    snapshots[0] if snapshots else None)
    incident_public = None
    if incident:
        base_runs = [row for row in STATE["runs"]
                     if row.get("skill") == sk["id"] and row.get("sid") == incident.get("id")
                     and not (row.get("variant") or {}) and not row.get("error")]
        representative = None
        if base_runs:
            groups = {}
            for row in base_runs:
                signature = json.dumps(row.get("facts") or {}, ensure_ascii=False,
                                       sort_keys=True, separators=(",", ":"))
                groups.setdefault(signature, []).append(row)
            representative = max(groups.values(), key=lambda rows: len(rows))[-1]
        issue = None
        if representative and sk.get("scenario_id") and incident.get("case_role") == "incident":
            issue = scenario_rt.analyze_issue(
                scenario_rt.get_pack(sk["scenario_id"]), incident, representative)
        incident_public = {
            "summary": incident.get("summary"), "task": incident.get("task"),
            "run_count": len(base_runs),
            "outcome": (representative or {}).get("outcome"),
            "facts": copy.deepcopy((representative or {}).get("facts") or {}),
            "issue": issue,
        }
    situations = []
    for row in scope_items(sk["id"]):
        snap = STATE["snapshots"].get(row.get("sid")) or {}
        situations.append({
            "id": row.get("id"), "case_id": snap.get("case_id"),
            "case_summary": snap.get("summary") or row.get("case_context"),
            "disposition": row.get("disposition"), "commitment": row.get("commitment"),
            "generator_exposure": row.get("generator_exposure"),
        })
    terminal = latest_task_end_review(sk["id"])
    questionnaire = next((row for row in reversed(STATE.get("questionnaires", []))
                          if row.get("skill") == sk["id"] and
                          row.get("review_id") == (terminal or {}).get("id") and
                          (not session or (sk.get("study_context") or {}).get("formal") or
                           row.get("session") == session)), None)
    progress = chat_progress(sk, session)
    return {
        "active": True,
        "skill": {"name": sk.get("name"), "version": sk.get("version"),
                  "scenario_id": sk.get("scenario_id"),
                  "instructions": copy.deepcopy(sk.get("instructions") or []),
                  "tools": [{"name": row.get("name"), "label": row.get("label"),
                             "signature": row.get("signature")}
                            for row in sk.get("tools") or []]},
        "work_order": copy.deepcopy(sk.get("work_order")),
        "study_context": copy.deepcopy(sk.get("study_context") or {}),
        "incident": incident_public,
        "scope_plan": current_scope_plan(sk),
        "repair_preview": public_repair_preview(sk),
        "review_round": sk.get("review_round") or 1,
        "scope_revision_required": bool(sk.get("scope_revision_required")),
        "scope": situations,
        "candidate": ({"name": sk["candidate"].get("name"),
                       "version": sk["candidate"].get("version"),
                       "author": sk["candidate"].get("author"),
                       "instructions": copy.deepcopy(
                           sk["candidate"].get("instructions") or []),
                       "tools": [{"name": row.get("name"), "label": row.get("label"),
                                  "signature": row.get("signature")}
                                 for row in sk["candidate"].get("tools") or []]}
                      if sk.get("candidate") else None),
        "terminal": ({"id": terminal.get("id"), "action": terminal.get("action"),
                      "version": terminal.get("version"), "reason": terminal.get("reason"),
                      "questionnaire_path": "/questionnaire",
                      "questionnaire_completed": bool(
                          questionnaire and questionnaire.get("status") == "completed")}
                     if terminal else None),
        "next_action": progress["next_action"],
        "progress": progress,
        "messages": [{"message": row.get("message"), "reply": row.get("reply")}
                     for row in STATE.get("chat", [])
                     if row.get("skill") == sk["id"] and
                     (not session or (sk.get("study_context") or {}).get("formal") or
                      row.get("session") == session)],
    }


def chat_progress(sk, session=""):
    """Return deterministic, chat-native guidance for the current decision point."""
    readiness = scope_readiness(sk)
    required = readiness.get("required_case_ids") or []
    missing = readiness.get("missing") or []
    required_count = len(required)
    completed_count = max(0, required_count - len(missing)) if required_count else 0
    terminal = latest_task_end_review(sk.get("id"))
    preview = current_repair_preview(sk)
    questionnaire = next((row for row in reversed(STATE.get("questionnaires", []))
                          if row.get("skill") == sk.get("id") and
                          row.get("review_id") == (terminal or {}).get("id") and
                          (not session or (sk.get("study_context") or {}).get("formal") or
                           row.get("session") == session)), None)
    if terminal:
        next_action = ("本轮任务和问卷均已完成。" if questionnaire and
                       questionnaire.get("status") == "completed" else
                       "本轮决定已记录，请完成任务问卷。")
    elif sk.get("candidate") and sk.get("last_compare"):
        next_action = "是否发布当前候选版本？也可以继续调整或暂缓。"
    elif sk.get("candidate"):
        next_action = "正在检查候选版本的影响。"
    elif sk.get("scope_revision_required"):
        next_action = "请说明这一轮需要调整的适用范围。"
    elif pending_chat_manifest(sk):
        next_action = "正在生成候选版本。"
    elif readiness["ready"]:
        next_action = "正在准备候选版本并检查修改影响。"
    elif readiness.get("duplicate_case_ids"):
        next_action = "同一情况存在多个判断。请确认最终处理原则。"
    elif completed_count:
        next_action = "剩余 %d 种情况需要确认。" % len(missing)
    else:
        next_action = "这类情况以后应当怎样处理？"
    return {
        "completed_cases": completed_count,
        "required_cases": required_count,
        "remaining_cases": len(missing),
        "next_action": next_action,
        "terminal": bool(terminal),
    }


def chat_guidance_text(progress):
    """State only the decision the owner currently needs to make."""
    return progress["next_action"]


def find_manifest(sk, manifest_id):
    return next((row for row in sk.get("manifests", []) if row.get("id") == manifest_id), None)


def candidate_record(sk, generated, author, manifest=None):
    """Create a candidate without dropping scenario, scope, or regression state."""
    exposures = []
    for item in (manifest or {}).get("visible_commitments", []):
        exposures.append({"situation_id": item["id"], "owner": "seen",
                          "candidate_author": "seen"})
    for item in (manifest or {}).get("withheld_commitments", []):
        exposures.append({"situation_id": item["id"], "owner": "seen",
                          "candidate_author": "withheld" if author == "ai" else "seen"})
    for item in (manifest or {}).get("excluded_cases", []):
        exposures.append({"situation_id": item["id"], "owner": "seen",
                          "candidate_author": "withheld" if author == "ai" else "seen",
                          "scope_role": "excluded-case-triage"})
    candidate = {
        "id": sk["id"],
        "name": sk["name"],
        "instructions": generated["instructions"],
        "tools": copy.deepcopy(sk.get("tools") or []),
        "sources": copy.deepcopy(sk.get("sources") or {}),
        "config": copy.deepcopy(sk.get("config") or {}),
        "version": sk["version"] + 1,
        "versions": [],
        "candidate": None,
        "rationale": generated.get("rationale", []),
        "generation_validation": copy.deepcopy(generated.get("_generation_validation")),
        "author": author,
        "scope_version": sk.get("scope_version") or 1,
        "scope_history": copy.deepcopy(sk.get("scope_history") or []),
        "scope_plans": copy.deepcopy(sk.get("scope_plans") or []),
        "active_scope_plan_id": sk.get("active_scope_plan_id"),
        "repair_previews": copy.deepcopy(sk.get("repair_previews") or []),
        "active_repair_preview_id": sk.get("active_repair_preview_id"),
        "review_round": sk.get("review_round") or 1,
        "candidate_rounds": copy.deepcopy(sk.get("candidate_rounds") or []),
        "scope_revision_required": False,
        "first_candidate_revealed_at": sk.get("first_candidate_revealed_at"),
        "manifests": copy.deepcopy(sk.get("manifests") or []),
        "regression_cases": copy.deepcopy(sk.get("regression_cases") or []),
        "scenario_id": sk.get("scenario_id"),
        "scenario_pack_hash": sk.get("scenario_pack_hash"),
        "input_manifest": copy.deepcopy(manifest),
        "case_exposure": exposures,
        "created_at": time.time(),
    }
    return initialize_skill_record(candidate)


def save_owner_candidate(sk, instructions, condition="workspace-owner"):
    """Commit exact owner-authored text and invalidate AI-authored provenance."""
    cand = sk.get("candidate")
    if not cand:
        readiness = scope_readiness(sk)
        if not readiness.get("ready") or sk.get("scope_revision_required"):
            raise ValueError("请先完成本轮适用范围确认")
        preview = ensure_repair_preview(sk)
        mark_repair_preview_presented(sk)
        manifest = compile_manifest(sk, condition)
        manifest["confirmed_at"] = time.time()
        manifest["commitment_source"] = "owner-authored-scope"
        confirm_repair_preview(sk, condition + "-candidate-input")
        cand = candidate_record(sk, {"instructions": [], "rationale": []}, "owner", manifest)
    cand["instructions"] = [{"n": row.get("n"), "text": (row.get("text") or "").strip()}
                            for row in instructions if (row.get("text") or "").strip()]
    cand["hash"] = shash(cand["instructions"])
    cand["content_hash"] = full_hash(cand["instructions"])
    cand["author"] = "owner"
    cand["scope_version"] = sk.get("scope_version") or 1
    cand["rationale"] = []
    cand["edited_at"] = time.time()
    for exposure in cand.get("case_exposure") or []:
        if exposure.get("candidate_author") == "withheld":
            exposure["candidate_author"] = "owner-exposed-after-generation"
    with LOCK:
        sk["candidate"] = cand
    record_semantic_event("candidate_revealed", sk, {
        "candidate_hash": cand.get("content_hash"),
        "scope_version": sk.get("scope_version") or 1,
        "author": "owner",
    })
    return cand


def _candidate_feedback(rows):
    return [{
        "expectation": row.get("expectation"),
        "disposition": row.get("disposition"),
        "result": ("failed" if row.get("verdict") in ("unmet", "broken") else
                   "needs_judgment" if row.get("verdict") == "needs_judgment" else
                   "insufficient" if row.get("verdict") == "insufficient" else "ok"),
    } for row in rows or []]


def archive_candidate_round(sk, reason):
    candidate = sk.get("candidate") if sk else None
    if not candidate:
        return None
    record = {
        "id": nid("v"), "review_round": sk.get("review_round") or 1,
        "reason": reason, "candidate": copy.deepcopy(candidate),
        "comparison": copy.deepcopy(sk.get("last_compare") or []),
        "comparison_budget": sk.get("last_compare_budget"),
        "scope_version": sk.get("scope_version") or 1,
        "scope_hash": (sk.get("scope_history") or [{}])[-1].get("hash"),
        "repair_preview_id": sk.get("active_repair_preview_id"),
        "archived_at": time.time(),
    }
    with LOCK:
        sk.setdefault("candidate_rounds", []).append(record)
    return copy.deepcopy(record)


def begin_candidate_revision(sk, reason="candidate-revision"):
    """Archive one candidate while retaining the locked behavioral scope."""
    archived = archive_candidate_round(sk, reason)
    if not archived:
        return {"error": "没有可修改的候选"}
    feedback = _candidate_feedback(archived.get("comparison") or [])
    with LOCK:
        sk["candidate"] = None
        sk["last_compare"] = None
        sk["last_compare_budget"] = None
        sk["pending_candidate_feedback"] = feedback
    return {"archived_candidate_id": archived.get("id"),
            "review_round": sk.get("review_round") or 1,
            "scope_version": sk.get("scope_version") or 1,
            "feedback": feedback}


def begin_scope_revision(sk, reason="post-reveal-scope-revision"):
    """Start a new scope round without rewriting history from the revealed candidate."""
    archived = archive_candidate_round(sk, reason)
    if not archived:
        return {"error": "需要先有一个已经检查过的候选"}
    old_rows = scope_items(sk["id"])
    if not old_rows:
        return {"error": "当前没有可继承的评审范围"}
    now = time.time()
    new_round = (sk.get("review_round") or 1) + 1
    clones = []
    for row in old_rows:
        clone = copy.deepcopy(row)
        clone.update({
            "id": nid("t"), "review_round": new_round,
            "copied_from": row.get("id"), "created": now, "judged_at": now,
            "pre_reveal": False, "post_reveal": True,
            "judged_before_candidate_in_round": True,
            "candidate_outcome_revealed_at": None,
        })
        clone.pop("superseded_at", None)
        clone.pop("superseded_by_plan", None)
        clones.append(clone)
    old_ids = {row.get("id") for row in old_rows}
    with LOCK:
        for row in STATE["situations"]:
            if row.get("skill") == sk["id"] and row.get("id") in old_ids and \
                    not row.get("superseded_at"):
                row["superseded_at"] = now
                row["superseded_by_round"] = new_round
        STATE["situations"].extend(clones)
        sk["candidate"] = None
        sk["last_compare"] = None
        sk["last_compare_budget"] = None
        sk["pending_candidate_feedback"] = []
        sk["review_round"] = new_round
        sk["scope_revision_required"] = True
        sk["active_repair_preview_id"] = None
        sk["scope_version"] = (sk.get("scope_version") or 1) + 1
        scope_record = freeze_scope_version(sk)
    return {"review_round": new_round, "scope_version": sk["scope_version"],
            "scope_hash": scope_record["hash"], "situations": copy.deepcopy(clones),
            "archived_candidate_id": archived.get("id")}


def comparison_from_outcome(rows):
    """Normalize workspace-owned comparison rows for revision provenance."""
    normalized = []
    for row in rows or []:
        if row.get("execution_failed") or row.get("insufficient"):
            verdict = "insufficient"
        elif row.get("conflict"):
            verdict = "broken" if row.get("disposition") == "preserve" else "unmet"
        elif row.get("needs_judgment"):
            verdict = "needs_judgment"
        elif row.get("disposition") == "change":
            verdict = "met"
        elif row.get("disposition") == "preserve":
            verdict = "kept"
        else:
            verdict = "untouched"
        normalized.append({
            "situation_id": row.get("situation_id"), "case_id": row.get("case_id"),
            "expectation": row.get("expectation"),
            "disposition": row.get("disposition"), "verdict": verdict,
            "execution_failed": bool(row.get("execution_failed")),
        })
    return normalized


def release_evidence(sk, candidate):
    rows = []
    comparison_budget = int(sk.get("last_compare_budget") or MATCHED_RUNS)
    for situation in scope_items(sk["id"]):
        sid = situation.get("sid")
        snap = STATE["snapshots"].get(sid) or {}
        before = [run["id"] for run in STATE["runs"]
                  if run.get("skill") == sk["id"] and run.get("sid") == sid
                  and not (run.get("variant") or {})
                  and run.get("artifact_hash") == sk.get("content_hash")
                  and run.get("snapshot_hash") == (snap.get("world_hash") or run.get("snapshot_hash"))]
        after = [run["id"] for run in STATE["runs"]
                 if run.get("skill") == sk["id"] and run.get("sid") == sid
                 and (run.get("variant") or {}).get("draft")
                 and not (run.get("variant") or {}).get("mask")
                 and not (run.get("variant") or {}).get("rewrite")
                 and not (run.get("variant") or {}).get("perturb")
                 and run.get("artifact_hash") == candidate.get("content_hash")
                 and run.get("snapshot_hash") == (snap.get("world_hash") or run.get("snapshot_hash"))]
        evaluator_hash = None
        if snap.get("scenario_id") and snap.get("case_id"):
            pack = scenario_rt.get_pack(snap["scenario_id"])
            evaluator_hash = full_hash(
                scenario_rt.get_case(pack, snap["case_id"]).get("oracle") or {})
        elif situation.get("criterion"):
            evaluator_hash = full_hash(situation["criterion"])
        before = before[-comparison_budget:]
        after = after[-comparison_budget:]
        rows.append({
            "situation_id": situation["id"],
            "disposition": situation["disposition"],
            "criterion": copy.deepcopy(situation.get("criterion")),
            "evaluator_hash": evaluator_hash,
            "snapshot": sid,
            "case_hash": snap.get("case_hash"),
            "world_hash": snap.get("world_hash"),
            "tool_schema_hash": snap.get("tool_schema_hash"),
            "baseline_runs": before,
            "candidate_runs": after,
            "candidate_outcome_revealed_at": situation.get("candidate_outcome_revealed_at"),
        })
    manifest = candidate.get("input_manifest") or {}
    source_ids = {row.get("id") for row in manifest.get("source_evidence") or []}
    active_case_ids = {(STATE["snapshots"].get(row.get("sid")) or {}).get("case_id")
                       for row in scope_items(sk["id"])}
    return {
        "manifest_id": (candidate.get("input_manifest") or {}).get("id"),
        "manifest_hash": (candidate.get("input_manifest") or {}).get("hash"),
        "scope_plan_id": (candidate.get("input_manifest") or {}).get("scope_plan_id"),
        "scope_plan_hash": (candidate.get("input_manifest") or {}).get("scope_plan_hash"),
        "repair_preview_id": (candidate.get("input_manifest") or {}).get("repair_preview_id"),
        "repair_preview_hash": (candidate.get("input_manifest") or {}).get("repair_preview_hash"),
        "review_round": (candidate.get("input_manifest") or {}).get("review_round"),
        "matched_runs_per_artifact": comparison_budget,
        "candidate_author": candidate.get("author"),
        "case_exposure": copy.deepcopy(candidate.get("case_exposure") or []),
        "runtime": scenario_rt.RUNTIME_SCHEMA if sk.get("scenario_id") else "legacy-simulator",
        "model": MODEL,
        "review_temperature": REVIEW_TEMPERATURE,
        "agent_temperature": AGENT_TEMPERATURE,
        "scenario_pack_hash": sk.get("scenario_pack_hash"),
        "scope_version": sk.get("scope_version"),
        "scope_hash": (sk.get("scope_history") or [{}])[-1].get("hash"),
        "source_interventions": [row.get("id") for row in STATE["probes"]
                                 if row.get("skill") == sk["id"] and
                                 row.get("id") in source_ids],
        "candidate_interventions": [row.get("id") for row in STATE["probes"]
                                    if row.get("skill") == sk["id"] and
                                    row.get("kind") == "block" and
                                    row.get("baseline_artifact_hash") ==
                                    candidate.get("content_hash") and
                                    row.get("case_id") in active_case_ids],
        "cases": rows,
    }


def release_readiness(sk, outcome, reason):
    """Reject releases that bypass whole-candidate review or hide a waiver."""
    if not sk or not sk.get("candidate"):
        return "没有草稿"
    situations = scope_items(sk["id"])
    if not situations:
        return "还没有确认修复范围"
    expected_ids = {row.get("id") for row in situations}
    outcome_ids = {row.get("situation_id") for row in outcome or []
                   if row.get("situation_id")}
    if outcome_ids != expected_ids:
        return "请先在全部已记录情况上完成修改前后检查"
    if any(row.get("execution_failed") for row in outcome or []):
        return "发布前执行失败：请重新运行完整的修改前后检查"
    evidence = release_evidence(sk, sk["candidate"])
    if any(not row["baseline_runs"] or not row["candidate_runs"]
           for row in evidence["cases"]):
        return "发布证据不完整：每个情况都需要原版本与完整候选运行"
    budget = evidence.get("matched_runs_per_artifact") or MATCHED_RUNS
    if any(len(row["baseline_runs"]) != budget or len(row["candidate_runs"]) != budget
           for row in evidence["cases"]):
        return "发布证据不完整：每个情况的原版本与完整候选需要使用同一固定预算"
    warnings = [row for row in outcome or []
                if row.get("conflict") or row.get("needs_judgment") or row.get("insufficient")]
    if warnings and not (reason or "").strip():
        return "仍有 mismatch、Needs judgment 或证据不足，请记录发布理由"
    return None


def regression_assets(sk):
    assets = list(copy.deepcopy(sk.get("regression_cases") or []))
    known = {row.get("situation_id") for row in assets}
    for situation in scope_items(sk["id"]):
        if situation.get("disposition") not in ("change", "preserve") or situation["id"] in known:
            continue
        snap = STATE["snapshots"].get(situation.get("sid")) or {}
        assets.append({
            "situation_id": situation["id"],
            "disposition": situation["disposition"],
            "commitment": situation["commitment"],
            "criterion": copy.deepcopy(situation.get("criterion")),
            "case_id": snap.get("case_id"),
            "case_hash": snap.get("case_hash"),
            "world_hash": snap.get("world_hash"),
            "added_from_version": sk.get("version"),
        })
    return assets


def publish_candidate(sk, reason, outcome, condition):
    candidate = sk.get("candidate")
    if not candidate:
        raise ValueError("没有草稿")
    review_id = nid("v")
    evidence = release_evidence(sk, candidate)
    history = list(sk.get("versions") or [])
    history.append({
        "name": sk["name"], "instructions": copy.deepcopy(sk["instructions"]),
        "version": sk["version"], "hash": sk["hash"],
        "content_hash": sk.get("content_hash"), "scope_version": sk.get("scope_version"),
    })
    candidate["versions"] = history
    candidate["candidate"] = None
    candidate["regression_cases"] = regression_assets(sk)
    candidate["released_at"] = time.time()
    review = {
        "id": review_id, "skill": sk["id"], "action": "publish",
        "reason": reason.strip(), "at": candidate["released_at"],
        "skill_hash": sk["hash"], "skill_content_hash": sk.get("content_hash"),
        "candidate_hash": candidate["hash"],
        "candidate_content_hash": candidate.get("content_hash"),
        "version": candidate["version"], "scope_version": sk.get("scope_version") or 1,
        "scope_hash": evidence.get("scope_hash"), "condition": condition,
        "situations": [{"id": x["id"], "commitment": x["commitment"],
                        "disposition": x["disposition"], "sealed": bool(x.get("sealed")),
                        "generator_exposure": x.get("generator_exposure")}
                       for x in scope_items(sk["id"])],
        "outcome": copy.deepcopy(outcome or []),
        "evidence": evidence,
    }
    review["record_hash"] = full_hash({k: v for k, v in review.items() if k != "record_hash"})
    with LOCK:
        STATE["skills"][sk["id"]] = candidate
        STATE.setdefault("reviews", []).append(review)
    record_semantic_event("decision_submitted", candidate, {
        "review_id": review_id, "action": "publish",
        "candidate_hash": candidate.get("content_hash"),
    })
    record_semantic_event("task_completed", candidate, {
        "review_id": review_id, "action": "publish",
    })
    return candidate, review


# ---------------------------------------------------------------- model

def _once(system, user, max_tokens):
    body = json.dumps({"model": MODEL, "temperature": REVIEW_TEMPERATURE,
                       "thinking": {"type": "disabled"},
                       "response_format": {"type": "json_object"}, "max_tokens": max_tokens,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + API_KEY})
    with urllib.request.urlopen(req, timeout=200) as resp:
        data = json.load(resp)
    out = json.loads(data["choices"][0]["message"]["content"])
    out["_usage"] = data.get("usage", {}).get("total_tokens", 0)
    return out


def ask(system, user, max_tokens=2500, attempts=3):
    last = None
    for a in range(attempts):
        try:
            return _once(system, user, max_tokens)
        except Exception as e:  # noqa: BLE001
            last = {"error": "%s: %s" % (type(e).__name__, str(e)[:180])}
            time.sleep(1.0 * (a + 1))
    return last


def _tool_turn_once(messages, tools, max_tokens=2500):
    """One OpenAI-compatible tool-calling turn; no JSON self-reporting."""
    body = json.dumps({
        "model": MODEL,
        "temperature": AGENT_TEMPERATURE,
        "thinking": {"type": "disabled"},
        "max_tokens": max_tokens,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + API_KEY})
    with urllib.request.urlopen(req, timeout=200) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"], data.get("usage", {}).get("total_tokens", 0)


def tool_turn(messages, tools, max_tokens=2500, attempts=3):
    last = None
    for attempt in range(attempts):
        try:
            return _tool_turn_once(messages, tools, max_tokens)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise last


# ---------------------------------------------------------------- prompts

P_PARSE = ("Parse a pasted agent skill document. Extract the numbered instructions in order, the "
           "tool declarations, and any configuration values stated in the text. Mark "
           "side_effecting=true for a tool that changes external state (booking, sending, deleting, "
           "paying). Classify every tool by kind: \"data\" = a dataset the user owns and the agent reads (calendar, preferences, policy, profile, contacts); \"query\" = a lookup against an outside service whose answer depends on parameters (search, pricing, availability); \"write\" = changes external state. Keep instruction text verbatim. For name, use the document's own title exactly as written, without translating or normalising it. Reply in the document's own language. Output "
           'ONLY JSON: {"name":"short name","instructions":[{"n":1,"text":""}],'
           '"tools":[{"name":"","label":"a short human name in the document\'s language, e.g. 我的日程 / 航班检索 / 预订，never the identifier",'
           '"signature":"","returns":"short shape description",'
           '"kind":"data|query|write","side_effecting":false}],"config":{}}')

P_SNAP = ("You are the environment in which an agent operates. Given the tool signatures and the "
          "user's task, produce realistic return values for every tool the agent will plausibly "
          "call. Requirements: concrete values; internally consistent; realistically varied so that "
          "no single option is trivially correct; include the ordinary trade-offs a real situation "
          "would contain. CRITICAL: the returned data must contain every field the skill's "
          "instructions refer to (if an instruction ranks by price, every option must carry a price; "
          "if it checks a threshold, the relevant amount must be present), otherwise the agent "
          "cannot execute. Do not reason about what the agent should choose. Output ONLY JSON: "
          '{"tools":{"<tool name>":<return value>},"summary":"one neutral sentence naming the '
          'situation, without evaluating any option"}')

P_ARGS = ("Given a user task, the user's own data, and one lookup tool, produce only the parameters "
          "that tool should be called with. Resolve relative expressions (tomorrow, next week) and "
          "implicit locations against the user's data so the parameters are consistent with it. "
          "If a sample record from the service is provided, use exactly its field names, its value "
          "vocabulary (codes rather than names, if that is what it uses) and its date format, and "
          "keep any range bound inside the period the sample covers. "
          "Do not answer the task. Output ONLY JSON: {\"args\":{}}")

P_CONNECT = ("You are an external service responding to one query. You do not know why it was asked. "
             "Return realistic, concrete results for exactly these parameters, with the ordinary "
             "variety a real service would return. Output ONLY JSON: {\"result\":<value>}")

P_EXEC = ("You are the agent executing the numbered skill exactly as written. Apply each "
          "instruction literally and in order. Do not introduce criteria the instructions do not "
          "state. If an instruction requires asking the user, record that as a gate step and do not "
          "assume a reply. State-changing tools execute inside the isolated review environment and "
          "return normal observable results. Reply in the skill's language for title/detail/outcome. Output ONLY "
          'JSON: {"steps":[{"kind":"tool|filter|decision|gate|action","title":"<=10 chars",'
          '"detail":"<=24 chars","from":<instruction number>}],"facts":{},"outcome":"one sentence"}')

P_FACTS = ("List the field names that capture the decision-relevant OUTCOME of this kind of task, "
           "so repeated executions can be compared field by field. The FIRST key must identify what "
           "the agent selected or decided. Never include fields that merely echo the input (the "
           "thing that was cancelled, the requested route, the date asked for) — those are identical "
           "in every execution and cannot distinguish one outcome from another. Prefer 5-8 short "
           'snake_case keys. Output ONLY JSON: {"keys":["..."]}')

P_INTENT = ("Route one message from a user of a skill-review tool. Intents: run (execute the skill "
            "on a new task), object (the last result was wrong), expect (state the behaviour they "
            "want), ask (a question about why something happened), probe (test whether an "
            "instruction matters), edit (change the instructions), publish (release the draft), "
            'other. Output ONLY JSON: {"intent":"","task":"<full task text if run>",'
            '"text":"<the substance>","instruction":<instruction number or null>}')

P_CRIT = ("Turn a user's stated expectation into checkable criteria over recorded executions. You "
          "are given the expectation, the observable fact keys the executions report, and the step "
          "kinds that appear. Propose 2-3 candidates of differing rigour. Forms: "
          'trace={"form":"trace","spec":{"must_exist":"<step kind or null>",'
          '"before":"<step kind or null>","must_call":"<exact tool name or null>",'
          '"before_tool":"<exact tool name or null>"}};'
          ' fact={"form":"fact","spec":{"key":"<fact key>","op":"==|!=|<|>|<=|>=","value":<literal>}}.'
          " Use only trace or fact criteria; never use a model judgment as an evaluator. "
          "Label in the user's language. Output ONLY JSON: "
          '{"candidates":[{"label":"short label","form":"","spec":{},"why":"one line"}]}')

P_INVERT = ("Rewrite one instruction so it directs the opposite behaviour, changing as little "
            "wording as possible and keeping the same grammatical shape and language. Used as a "
            "controlled probe: if behaviour does not flip, the instruction is not being followed. "
            'Output ONLY JSON: {"text":"the inverted instruction"}')

P_PERTURB = ("Choose one value inside the frozen tool results that the agent's decision might depend "
             "on, and propose an altered value that keeps the data realistic but would change the "
             "correct answer if that value actually mattered. Output ONLY JSON: "
             '{"tool":"<tool name>","path":"<path within that tool value, e.g. [0].start>",'
             '"from":<current value>,"to":<new value>,"why":"one line"}')

P_DRAFT = ("Revise the numbered skill so it implements the stated expectations. Each expectation "
           "carries a disposition: change = the behaviour must become this; preserve = this behaviour "
           "must keep working exactly as it does now, do not weaken it; unresolved = the owner has "
           "authorised no rule here, so do not silently decide it (keep current behaviour or ask at "
           "run time). Change as little as possible. Preserve instructions no expectation concerns. "
           "If at least one change expectation exists, an unchanged candidate is invalid: make a "
           "concrete textual edit that operationalizes its observable behavior. Never claim that "
           "unchanged wording implements a new priority or constraint. An unresolved boundary does "
           "not cancel a scoped change; route only that boundary to runtime judgment when needed. "
           "When existing wording directly conflicts with a change expectation, replace or remove "
           "the conflicting clause; do not keep the contradiction verbatim and merely append a "
           "'but' or 'however' exception. "
           "Never remove a confirmation or safety requirement unless an expectation with disposition "
           "change explicitly asks for it. Keep the original language and numbering. In each "
           "instruction object, n carries the number and text contains only the instruction body; "
           "never repeat a numeric label such as '1.' inside text. Every "
           "expectation and source-location probe has an id. For each changed instruction, cite only "
           "the commitment ids that justify the behavior and the source evidence ids actually used "
           "to choose the edit location. Source evidence does not validate the new text. Output ONLY JSON: "
           '{"instructions":[{"n":1,"text":""}],'
           '"rationale":[{"n":1,"why":"one line","commitment_ids":["t1"],'
           '"source_evidence_ids":["p1"]}]}')

P_TARGETED_EDIT = ("The complete skill revision repeatedly came back unchanged even though the "
                   "owner has a mandatory Change commitment. Make one minimal, concrete edit that "
                   "operationalizes that Change while respecting every Preserve and Unresolved "
                   "expectation. Prefer one of the source-located TARGET instruction numbers when "
                   "provided. Do not use or infer any hidden reference answer. Return exactly one "
                   "existing instruction number and its replacement text in the skill's language. "
                   "Replace any old clause that directly contradicts the Change; do not retain that "
                   "clause and append a conflicting exception. "
                   "The text field must contain only the instruction body, without repeating its "
                   "numeric label. "
                   'Output ONLY JSON: {"n":1,"text":"replacement instruction",'
                   '"why":"one line","commitment_ids":["t1"],'
                   '"source_evidence_ids":["p1"]}')

P_CONTRAST = ("A workflow owner has just said what they want changed about an agent's behaviour. "
              "Propose 2 nearby situations that the same repair could accidentally affect: one where "
              "an existing behaviour should most likely be kept as it is, and one where the right "
              "policy is genuinely unclear and the owner may not want any rule decided for them. "
              "Each must be a concrete situation for the same skill, described in one short sentence "
              "in the owner's language. Output ONLY JSON: "
              '{"situations":[{"text":"","suggest":"preserve|unresolved","why":"one line"}]}')

P_SCOPE_PLAN = (
    "Translate a workflow owner's requested behavior change into a small, executable boundary-review "
    "plan. First state the intent as observable business behavior: the trigger, required action, "
    "forbidden action, and any genuinely undefined term or lifecycle boundary. Then select only from "
    "the supplied CANDIDATE CASES; never invent a case id, tool result, threshold, policy, or answer. "
    "Select two cases by default and a third only when it tests a distinct part of the owner's rule. "
    "Distinguish a direct counterexample or definition boundary from an independent existing safeguard. "
    "For each selected case, explain in the owner's language exactly which phrase or component of the "
    "requested change it tests. If a term such as 'non-compliant' is broader than the available policy "
    "fact, ask a neutral scope question instead of treating an unlisted category as restricted. Do not "
    "resolve an open business policy for the owner. Ground requested actions in AVAILABLE PRODUCT "
    "ACTIONS. If the owner's term differs from the available action, phrase the summary as using that "
    "available action and list the mapping as an ambiguity rather than inventing another tool. Output "
    "ONLY JSON: "
    '{"intent":{"summary":"one faithful sentence","trigger":"","required_action":"",'
    '"forbidden_action":"","ambiguities":[""]},"cases":[{"case_id":"",'
    '"relation_type":"outside-trigger|existing-safeguard|definition-boundary|policy-limit|action-boundary|related-case",'
    '"why_relevant":"one concrete sentence","owner_question":"one neutral question or empty"}]}')

P_SOURCE_PLAN = (
    "Choose one bounded source-instruction check that can help a workflow owner locate where the "
    "original skill is sensitive to the already confirmed behavioral scope. Select exactly one "
    "existing instruction number and one already executed case id from the supplied allowlists. "
    "Prefer the motivating incident and the smallest instruction whose wording directly creates, "
    "orders, or fails to guard the observed behavior. This is an edit-location cue, not a causal "
    "claim and not evidence that any future candidate will work. Never write a new policy, choose a "
    "desired outcome, inspect an oracle, or invent an instruction or case. Explain the conditional "
    "question in the owner's language without naming deletion, inversion, perturbation, or other "
    "internal mechanics, and cite only supplied commitment ids. Output ONLY JSON: "
    '{"probe":{"instruction":1,"case_id":"","question":"If this source instruction is '
    'changed, does it alter the observable behavior that matters in this case?",'
    '"commitment_ids":["t1"]}}')

P_CASE = ("Create one realistic neighbouring case for an agent workflow from a recorded base case. "
          "The requested situation describes the difference the owner wants to inspect. Return a "
          "complete task and complete frozen tool results for that new case. Keep exactly the same "
          "top-level tool names and data shapes as the base. Change only the one or two values needed "
          "to make the requested situation concrete; preserve unrelated facts. This must be a genuine "
          "new case, not a paraphrase or copy of the base data. Do not decide what the agent should do "
          "and do not mention any expected policy. Reply in the owner's language. Output ONLY JSON: "
          '{"task":"one concrete user task","tools":{},"summary":"one neutral sentence",'
          '"changed_factors":["short factual change"]}')

P_JUDGE = ("Answer a yes/no question about one recorded execution. Use only the recorded steps, "
           'facts and outcome. Output ONLY JSON: {"pass":true|false,"why":"one line"}')


# ---------------------------------------------------------------- core

def normalize_generated_instruction_text(number, value):
    """Remove a model-repeated list label without turning formatting into a change."""
    text = (value or "").strip()
    marker = re.compile(r"^\s*%s\s*[\.\)）、．:：-]\s*" % re.escape(str(number)))
    while marker.match(text):
        text = marker.sub("", text, count=1).strip()
    return text

def generate_candidate(instructions, expectations, evidence, feedback=None, attempts=3):
    """Run the drafting prompt and reject malformed or no-op Change patches."""
    lines = "\n".join("%d. %s" % (row["n"], row["text"]) for row in instructions)
    extra = ("\n\nPREVIOUS ATTEMPT — what the last revision actually did. Fix these without "
             "losing what already works:\n" + json.dumps(feedback, ensure_ascii=False)) \
        if feedback else ""
    base_prompt = "SKILL:\n%s\n\nEXPECTATIONS:\n%s\n\nPROBE EVIDENCE:\n%s%s" % (
        lines, json.dumps(expectations, ensure_ascii=False),
        json.dumps(evidence, ensure_ascii=False), extra)
    must_change = any(row.get("disposition") == "change" for row in expectations)
    original_hash = full_hash(instructions)
    original_numbers = {row["n"] for row in instructions}
    original_by_number = {row["n"]: row["text"].strip() for row in instructions}
    source_targets = {row.get("instruction") for row in evidence
                      if isinstance(row.get("instruction"), int)}
    last_error = "候选格式无效"
    for attempt in range(1, max(1, attempts) + 1):
        prompt = base_prompt
        if attempt > 1:
            prompt = ("VALIDATION ERROR FROM THE PREVIOUS OUTPUT: %s. A Change commitment is "
                      "mandatory, so returning the original skill is invalid. First locate old "
                      "statements that conflict with Change, then rewrite them in the returned "
                      "complete list. An Unresolved commitment applies only to its specific "
                      "boundary and must not cancel the scoped Change.\n\n"
                      "EXPECTATIONS (higher priority than the old skill):\n%s\n\n"
                      "OLD SKILL TO REVISE:\n%s\n\nPROBE EVIDENCE:\n%s%s" % (
                          last_error, json.dumps(expectations, ensure_ascii=False), lines,
                          json.dumps(evidence, ensure_ascii=False), extra))
        out = ask(P_DRAFT, prompt, 4000)
        if not out or not isinstance(out.get("instructions"), list):
            last_error = "没有返回完整 instructions 数组"
            continue
        normalized = []
        valid = True
        for row in out["instructions"]:
            if not isinstance(row, dict) or not isinstance(row.get("n"), int) or \
                    not isinstance(row.get("text"), str) or not row["text"].strip():
                valid = False
                break
            clean_text = normalize_generated_instruction_text(row["n"], row["text"])
            if not clean_text:
                valid = False
                break
            normalized.append({"n": row["n"], "text": clean_text})
        normalized_numbers = {row["n"] for row in normalized}
        if not valid or len(normalized_numbers) != len(normalized):
            last_error = "指令必须有唯一整数编号和非空文本"
            continue
        if normalized_numbers != original_numbers:
            last_error = "候选必须保留原 skill 的完整指令编号集合"
            continue
        retained_target_clauses = [
            row["n"] for row in normalized
            if must_change and row["n"] in source_targets and
            row["text"] != original_by_number.get(row["n"]) and
            original_by_number.get(row["n"]) in row["text"]
        ]
        if retained_target_clauses:
            last_error = ("修改位置 %s 仍逐字保留旧条款再追加内容；请直接替换冲突条款" %
                          ", ".join(str(number) for number in retained_target_clauses))
            continue
        if must_change and full_hash(normalized) == original_hash:
            last_error = "存在 Change commitment，但候选与原 skill 逐字相同"
            continue
        out["instructions"] = normalized
        out["_generation_validation"] = {
            "attempts": attempt, "must_change": must_change,
            "review_temperature": REVIEW_TEMPERATURE,
        }
        return out
    if must_change:
        targets = sorted({row.get("instruction") for row in evidence
                          if isinstance(row.get("instruction"), int)})
        targeted_prompt = (
            "OLD SKILL:\n%s\n\nOWNER-CONFIRMED EXPECTATIONS:\n%s\n\n"
            "SOURCE-LOCATED TARGET NUMBERS:\n%s\n\nSOURCE EVIDENCE IDS:\n%s" % (
                lines, json.dumps(expectations, ensure_ascii=False),
                json.dumps(targets, ensure_ascii=False),
                json.dumps([row.get("id") for row in evidence], ensure_ascii=False)))
        targeted = ask(P_TARGETED_EDIT, targeted_prompt, 1400)
        old_by_number = {row["n"]: row["text"] for row in instructions}
        allowed_numbers = set(targets) if targets else set(old_by_number)
        number = targeted.get("n") if isinstance(targeted, dict) else None
        replacement = normalize_generated_instruction_text(
            number, targeted.get("text") or "") \
            if isinstance(targeted, dict) else ""
        if number in allowed_numbers and replacement and replacement != old_by_number[number] and \
                old_by_number[number] not in replacement:
            normalized = [{"n": row["n"],
                           "text": replacement if row["n"] == number else row["text"]}
                          for row in instructions]
            return {
                "instructions": normalized,
                "rationale": [{
                    "n": number, "why": targeted.get("why") or "定点落实 Change commitment",
                    "commitment_ids": targeted.get("commitment_ids") or [
                        row.get("id") for row in expectations
                        if row.get("disposition") == "change"],
                    "source_evidence_ids": targeted.get("source_evidence_ids") or [
                        row.get("id") for row in evidence
                        if row.get("instruction") == number],
                }],
                "_generation_validation": {
                    "attempts": max(1, attempts) + 1, "must_change": True,
                    "review_temperature": REVIEW_TEMPERATURE,
                    "recovery": "targeted-edit-after-noop",
                },
            }
    return {"error": "候选生成未通过服务端校验：%s" % last_error,
            "validation_attempts": max(1, attempts)}


def instructions_for(variant):
    sk = cur()
    base = sk["candidate"] if variant.get("draft") and sk.get("candidate") else sk
    mask = set(variant.get("mask") or [])
    rw = variant.get("rewrite") or {}
    out = []
    for i in base["instructions"]:
        if i["n"] in mask:
            continue
        out.append({"n": i["n"], "text": rw["text"]} if rw and i["n"] == rw.get("n") else dict(i))
    return out, base


def apply_perturb(tools, pert):
    if not pert:
        return tools
    import copy
    t = copy.deepcopy(tools)
    name = pert.get("tool")
    if name not in t:
        return t
    parts = [p for p in re.split(r"[.\[\]]", pert.get("path", "")) if p != ""]
    if not parts:
        t[name] = pert.get("to")
        return t
    node = t[name]
    try:
        for p in parts[:-1]:
            node = node[int(p)] if p.isdigit() else node[p]
        last = parts[-1]
        if last.isdigit():
            node[int(last)] = pert.get("to")
        else:
            node[last] = pert.get("to")
    except Exception:  # noqa: BLE001
        pass
    return t



def _num(v):
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return None


def query_fixture(rows, args):
    """对后台固定表做一次确定性查询。
    参数名与行字段同名时按等值匹配；after/from/since/min 视为下界，
    before/to/until/max 视为上界；行里没有的字段一律忽略。"""
    if not isinstance(rows, list):
        return rows
    LOW = ("after", "from", "since", "min", "start")
    HIGH = ("before", "until", "to", "max", "end")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ok = True
        for k, v in (args or {}).items():
            if v in (None, ""):
                continue
            kl = str(k).lower()
            if k in r:
                a, b = norm(r[k]), norm(v)
                if a != b and b not in a:
                    ok = False
                    break
                continue
            bound = next((p for p in LOW + HIGH if kl.startswith(p) or kl.endswith(p)), None)
            if not bound:
                continue                      # 行里没有这个字段：忽略该条件
            base = kl
            for p in LOW + HIGH:
                base = base.replace(p, "")
            base = base.strip("_- ")
            fld = next((f for f in r if base and base in f.lower()), None)
            if fld is None:
                fld = next((f for f in r
                            if any(t in f.lower() for t in ("time", "date", "depart", "arriv"))),
                           None)
            if fld is None:
                continue
            rv, qv = r[fld], v
            nr, nq = _num(rv), _num(qv)
            if nr is not None and nq is not None:
                good = (nr >= nq) if bound in LOW else (nr <= nq)
            else:
                good = (str(rv) >= str(qv)) if bound in LOW else (str(rv) <= str(qv))
            if not good:
                ok = False
                break
        if ok:
            out.append(r)
    return out


def _legacy_exec_once(instructions, snap, pert=None):
    lines = "\n".join("%d. %s" % (i["n"], i["text"]) for i in instructions)
    tools = apply_perturb(snap["tools"], pert)
    schema = snap.get("fact_schema")
    extra = ("\n\nREPORT THESE FACT KEYS: " + ", ".join(schema)) if schema else ""
    user = "SKILL:\n%s\n\nTASK: %s\n\nTOOL RESULTS:\n%s%s" % (
        lines, snap["task"], json.dumps(tools, ensure_ascii=False, indent=1)[:6000], extra)
    out = ask(P_EXEC, user, 2500)
    if out and "error" not in out:
        out.setdefault("facts", {})
        out.setdefault("steps", [])
    return out


def _clean_tool_calls(calls):
    cleaned = []
    for call in calls or []:
        fn = call.get("function") or {}
        cleaned.append({
            "id": call.get("id"),
            "type": "function",
            "function": {"name": fn.get("name"), "arguments": fn.get("arguments") or "{}"},
        })
    return cleaned


def _scenario_exec_once(instructions, snap, pert=None):
    """Run an actual tool loop against an isolated scenario world."""
    pack = scenario_rt.get_pack(snap["scenario_id"])
    world = scenario_rt.perturb_world(pack, snap, pert, apply_perturb)
    lines = "\n".join("%d. %s" % (i["n"], i["text"]) for i in instructions)
    system = (
        "You are the agent responsible for completing a recurring workflow. Follow the reusable "
        "skill below as the authoritative procedure. Obtain all external facts by calling tools; "
        "never invent a tool result and never claim that a write occurred unless you called its "
        "tool. Do not add priorities or constraints that the skill does not state, and do not promote "
        "a consideration described as a trade-off into a hard constraint. Treat enumerated hard "
        "constraints as exhaustive: tool data supplies facts, not additional policy. Follow explicit "
        "ranking and ordering rules when considerations conflict. State-changing tools execute "
        "normally inside the isolated review environment, so call them when the skill requires the "
        "action. If the skill requires confirmation or approval, call the corresponding tool "
        "before the action. After the work is complete, give one concise outcome in the skill's "
        "language.\n\nREUSABLE SKILL:\n" + lines)
    user = "CURRENT TIME: %s\n\nTASK:\n%s" % (snap.get("clock"), snap["task"])
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    tools = scenario_rt.tool_definitions(pack)
    trace, usage, outcome = [], 0, ""

    try:
        for _ in range(MAX_TOOL_TURNS):
            message, used = tool_turn(messages, tools, 2500)
            usage += used
            calls = _clean_tool_calls(message.get("tool_calls"))
            if not calls:
                outcome = (message.get("content") or "").strip()
                break
            assistant = {"role": "assistant", "content": message.get("content") or "",
                         "tool_calls": calls}
            messages.append(assistant)
            for call in calls:
                fn = call["function"]
                try:
                    arguments = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                    result = {"ok": False, "error": "tool arguments were not valid JSON"}
                else:
                    result = scenario_rt.dispatch(pack, world, fn.get("name"), arguments)
                step = scenario_rt.tool_step(pack, fn.get("name"), arguments, result, instructions)
                step["call_id"] = call.get("id")
                step["sequence"] = len(trace) + 1
                trace.append(step)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(result, ensure_ascii=False),
                })
        else:
            outcome = "执行达到工具调用上限，尚未产生稳定结论。"
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, str(exc)[:180]),
                "steps": trace, "facts": {}, "_usage": usage}

    facts = scenario_rt.derive_facts(pack, world, trace)
    oracle = scenario_rt.evaluate_oracle(pack, snap["case_id"], facts)
    return {
        "steps": trace,
        "trace": trace,
        "facts": facts,
        "outcome": outcome or "已完成工具调用，模型未返回文字总结。",
        "_oracle": oracle,
        "_usage": usage,
        "execution": {
            "runtime": scenario_rt.RUNTIME_SCHEMA,
            "model": MODEL,
            "temperature": AGENT_TEMPERATURE,
            "max_tool_turns": MAX_TOOL_TURNS,
            "tool_schema_hash": snap.get("tool_schema_hash"),
            "world_hash": snap.get("world_hash"),
            "case_hash": snap.get("case_hash"),
            "tool_calls": len(trace),
            "facts_source": "tool-trace-and-world-state",
            "side_effect_policy": "isolated-sandbox",
        },
    }


def exec_once(instructions, snap, pert=None):
    if snap.get("runtime") == scenario_rt.RUNTIME_SCHEMA:
        return _scenario_exec_once(instructions, snap, pert)
    return _legacy_exec_once(instructions, snap, pert)


def execute_batch(instructions, snap, variant, k, criterion=None):
    """Execute and persist a non-streaming batch for the chat condition."""
    artifact_hash = full_hash(instructions)
    rows = []
    with concurrent.futures.ThreadPoolExecutor(k) as executor:
        futures = [executor.submit(exec_once, instructions, snap, variant.get("perturb"))
                   for _ in range(k)]
        for future in concurrent.futures.as_completed(futures):
            run = future.result() or {"error": "空返回"}
            run["_pass"] = eval_criterion(criterion, run) if criterion else None
            run.update({
                "id": nid("r"), "sid": snap["id"], "variant": copy.deepcopy(variant),
                "skill": STATE["active"], "artifact_hash": artifact_hash,
                "snapshot_hash": snap.get("world_hash") or full_hash({
                    "task": snap.get("task"), "tools": snap.get("tools")}),
            })
            rows.append(run)
    with LOCK:
        STATE["runs"].extend(rows)
    return rows


def eval_criterion(crit, run):
    if not crit or not run or "error" in run:
        return None
    form, spec = crit.get("form"), crit.get("spec") or {}
    if form == "trace":
        kinds = [s.get("kind") for s in run.get("steps", [])]
        need = spec.get("must_exist")
        if need and need not in kinds:
            return False
        before = spec.get("before")
        if before and need:
            if before in kinds and need not in kinds:
                return False
            if before in kinds and kinds.index(need) >= kinds.index(before):
                return False
        calls = [s.get("tool") for s in run.get("steps", [])]
        must_call = spec.get("must_call")
        if must_call and must_call not in calls:
            return False
        before_tool = spec.get("before_tool")
        if before_tool and must_call:
            if before_tool in calls and must_call not in calls:
                return False
            if before_tool in calls and calls.index(must_call) >= calls.index(before_tool):
                return False
        return True
    if form == "fact":
        f = run.get("facts") or {}
        key = spec.get("key")
        if key not in f:
            return None
        a, b, op = f[key], spec.get("value"), spec.get("op", "==")
        try:
            if op == "==":
                return a == b
            if op == "!=":
                return a != b
            if isinstance(a, str) and isinstance(b, str):
                return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
            a, b = float(a), float(b)
            return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
        except Exception:  # noqa: BLE001
            return None
    if form == "semantic":
        r = ask(P_JUDGE, "QUESTION: %s\n\nEXECUTION:\n%s" % (
            spec.get("question", ""), json.dumps(run, ensure_ascii=False)[:3000]), 900)
        return bool(r.get("pass")) if r and "error" not in r else None
    return None


def norm(v):
    """把模型输出的类型抖动归一：false/"false"/"False" 视为同一个值。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("true", "yes", "y"):
            return "true"
        if t in ("false", "no", "n", "none", "null"):
            return "false"
        return t
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def fact_signature(run, primary=None):
    """行为签名。给定主决策键时只比它 —— 「选了什么」是行为是否改变的判准；
    是否满足预期由判据单独负责，两者不混。"""
    if not run or "error" in run:
        return None
    f = run.get("facts") or {}
    if primary:
        for k in primary:
            if k in f:
                return norm(f[k])
    sig = {}
    for k in sorted(f):
        v = f[k]
        if isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 24
                                                 and not re.match(r"^\d{4}-\d{2}-\d{2}T", v)):
            sig[k] = norm(v)
    return json.dumps(sig, ensure_ascii=False) if sig else None


def modal(sigs):
    """返回 (众数, 占比)。忽略 None。"""
    xs = [x for x in sigs if x]
    if not xs:
        return None, 0.0
    best = max(set(xs), key=xs.count)
    return best, xs.count(best) / float(len(xs))


def _scalars(run):
    """一次执行里可用于比较的短标量字段。"""
    out = {}
    for k, v in (run.get("facts") or {}).items():
        if isinstance(v, (bool, int, float)) or (
                isinstance(v, str) and len(v) <= 40
                and not re.match(r"^\d{4}-\d{2}-\d{2}T", v)):
            out[k] = norm(v)
    return out


def discriminating(*groups):
    """在若干组执行的并集里，找出取值会变化的字段。
    在所有执行中恒定的字段（例如被取消的原航班号）无法区分行为，必须排除。"""
    runs = [r for g in groups for r in (g or []) if r and "error" not in r]
    if not runs:
        return None
    vals = {}
    for r in runs:
        for k, v in _scalars(r).items():
            vals.setdefault(k, set()).add(v)
    keys = sorted(k for k, s in vals.items() if len(s) > 1)
    return keys or None


def field_stats(runs, keys):
    """逐字段统计：每个可区分字段的主导取值与占比。
    「选哪一个」和「有没有请求确认」是两个维度，混成一个签名会让一切都显得不稳定。"""
    out = {}
    for k in (keys or []):
        vals = [_scalars(r).get(k) for r in runs if r and "error" not in r]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        top = max(set(vals), key=vals.count)
        out[k] = {"top": top, "share": round(vals.count(top) / float(len(vals)), 2),
                  "n": len(vals)}
    return out


def compare_fields(base, probe, keys):
    """逐字段比较两组执行。只在双方都足够稳定的字段上给结论。
    返回 (是否改变, 依据字段, 是否证据不足)。"""
    a, b = field_stats(base, keys), field_stats(probe, keys)
    solid = [k for k in a if k in b and a[k]["share"] >= 0.6 and b[k]["share"] >= 0.6]
    if not solid:
        return None, [], True
    diff = [k for k in solid if a[k]["top"] != b[k]["top"]]
    return (len(diff) > 0), diff, False


def sig_by(run, keys):
    if not run or "error" in run:
        return None
    sc = _scalars(run)
    if not keys:
        return json.dumps(sc, ensure_ascii=False, sort_keys=True) if sc else None
    return json.dumps({k: sc[k] for k in keys if k in sc}, ensure_ascii=False, sort_keys=True)


def summarize(runs, crit=None, primary=None):
    """把 k 次执行归成若干「行为组」，并指出组间差异在哪些字段。
    分组只看在这批执行里取值会变化的字段：恒定字段无法区分行为。"""
    dk = discriminating(runs)
    groups, ok, tot, err = {}, 0, 0, 0
    for r in runs:
        if not r or "error" in r:
            err += 1
            continue
        sig = sig_by(r, dk) or (r.get("outcome") or "")[:60]
        g = groups.setdefault(sig, {"sig": sig, "n": 0, "outcome": "", "facts": {}, "steps": []})
        g["n"] += 1
        if not g["steps"]:
            g["outcome"] = r.get("outcome") or ""
            g["facts"] = r.get("facts") or {}
            g["steps"] = r.get("steps") or []
        if crit and r.get("_pass") is not None:
            tot += 1
            ok += 1 if r["_pass"] else 0

    gl = sorted(groups.values(), key=lambda g: -g["n"])
    # 组间差异字段：在不同组里取值不同的键
    diff_keys = []
    if len(gl) > 1:
        keys = set()
        for g in gl:
            keys |= set(g["facts"].keys())
        for k in sorted(keys):
            vals = {norm(g["facts"].get(k)) for g in gl}
            if len(vals) > 1:
                diff_keys.append(k)
    # 不猜分叉点：步骤标题是自由文本，措辞差异会被误判成行为差异。
    # 只报「另一种结局」，由决策字段的实际差异支撑。
    fork = None
    if len(gl) > 1:
        fork = {"alt_outcome": gl[1]["outcome"], "keys": diff_keys,
                "main_outcome": gl[0]["outcome"]}

    fields = field_stats(runs, dk)
    good = len(runs) - err
    group_share = round(gl[0]["n"] / float(good), 2) if gl and good else 0
    return {"groups": gl, "diff_keys": diff_keys, "fork": fork, "fields": fields,
            "n": len(runs), "good": good, "err": err,
            "pass": ok, "tot": tot,
            "top": gl[0]["sig"] if gl else None,
            "share": group_share,
            "group_share": group_share,
            "stable": bool(gl and gl[0]["n"] == good and good > 0)}


def verdict_for(n):
    if n is None:
        return None
    ps = {p["kind"]: p for p in mine(STATE["probes"]) if p.get("n") == n}
    d, i = ps.get("delete"), ps.get("invert")
    for p in (d, i):
        if p and p.get("confidence") == "unstable":
            return {"code": "unstable",
                    "text": "基线或探测本身波动较大（主导行为占比 %s/%s），结论不可靠 —— 提高重复次数后再看"
                            % (p.get("base_share"), p.get("probe_share"))}
        if p and p.get("confidence") == "no_baseline":
            return {"code": "nobase", "text": "还没有基线执行，无法比较"}
    if d and i:
        if not d["changed"] and i["changed"]:
            return {"code": "prior", "text": "这条规则与模型原有倾向一致；采用相反处理原则时，结果发生变化。"}
        if not d["changed"] and not i["changed"]:
            return {"code": "dead", "text": "当前检查尚未确认这条规则会稳定改变处理结果。"}
        if d["changed"] and i["changed"]:
            return {"code": "control", "text": "在当前情况与配置下，改变这条规则会稳定影响处理结果。"}
        return {"code": "noisy", "text": "两次探测结论不一致，需要提高重复次数"}
    if d:
        return {"code": "partial", "text": "不应用这条规则时%s；还需要另一种规则对照才能判断关联。"
                % ("结果发生变化" if d["changed"] else "结果没有变化")}
    if i:
        return {"code": "partial", "text": "采用相反处理原则时%s" %
                ("结果发生变化" if i["changed"] else "结果没有变化")}
    return None



# ---------------------------------------------------------------- 条件 B：内部操作
# 与条件 A 共用同一批核心函数。差别只在交互层：这里没有持久面板，结果以叙述返回。

P_ROUTE = ("Route one message from someone reviewing an agent skill in a chat-only product. "
           "Pick exactly one capability, or none. Capabilities: "
           "run_task(task) execute the skill on a task; "
           "show_options() describe the candidate set of the last run and which was chosen; "
           "check_instruction(n) test whether instruction n affects behaviour; "
           "check_candidate_block(n,case_id?) temporarily mask one committed candidate instruction "
           "against the candidate baseline after a mismatch; "
           "check_data() test whether the supplied data entered the decision; "
           "suggest_cases() show nearby situations that may be affected; "
           "open_case(case_id) execute one previously suggested situation; "
           "record_expectation(text, disposition=change|preserve|unresolved|excluded) record what the owner wants; "
           "draft(confirm=false) preview the exact drafting inputs, or confirm that preview and have "
           "the model revise the instructions when the user explicitly confirms; "
           "edit_instruction(n,text) replace one numbered instruction with owner-authored text; "
           "compare() run before and after on every recorded expectation; "
           "reopen_scope() archive the revealed candidate and start a new scope round when the owner "
           "explicitly wants to change the behavioral boundary; "
           "decide(action=publish|revise|gather|defer, reason); "
           "list_instructions(); none. "
           "The product automatically advances the normal review workflow, so use these capabilities "
           "only for an explicit side question, manual edit, or final release decision. "
           "Do not invent a capability. Do not act unless the message asks for it. "
           'Output ONLY JSON: {"capability":"","args":{}}')

P_NARRATE = ("You are a chat-only assistant for reviewing an agent skill. Describe the result below "
             "in prose, in the user's language. Be accurate and complete but do not render tables, "
             "bullet lists of more than three items, or any structure the user could click. State "
             "uncertainty when the result is marked unstable or insufficient. Two to five sentences. "
             'Output ONLY JSON: {"reply":""}')

P_ASKPRESERVE = ("Before revising the instructions, ask the owner once, in one short question, which "
                 "existing behaviours must keep working unchanged. Do not list candidates for them. "
                 'Output ONLY JSON: {"reply":""}')

P_SCOPE_PARSE = (
    "Extract explicit business-policy decisions from the workflow owner's reply. The CASES JSON is "
    "the complete set that may be recorded in this turn. A case may include a product proposal that "
    "the assistant already showed to the owner. Never infer a decision merely from the case facts, "
    "the current outcome, or the proposal. The owner may refer to cases by number, description, or "
    "plain language. Use change when they want behaviour different from the recorded current outcome, "
    "preserve when they want an existing behaviour kept, unresolved when they deliberately leave "
    "the policy open or require case-by-case human judgment, and excluded only when they explicitly say "
    "a suggested case is irrelevant to this repair. Excluded is case triage, not a policy commitment. "
    "Keep each commitment concrete and faithful "
    "to the owner's words; do not add thresholds or policy. If the owner explicitly accepts all proposals, "
    "or answers only the open-boundary question after being told that doing so accepts the other proposals, "
    "set accept_proposals=true. Questions, requests for more evidence, and vague acknowledgements are not "
    "policy decisions. Return only case_ids present in CASES. Reply ONLY as JSON: "
    '{"understood":true|false,"accept_proposals":true|false,'
    '"decisions":[{"case_id":"","disposition":"change|preserve|unresolved|excluded",'
    '"commitment":""}],"clarification":"one short question or empty"}')


def op_run(task, k=BASELINE_RUNS):
    sk = cur()
    if sk.get("scenario_id"):
        pack = scenario_rt.get_pack(sk["scenario_id"])
        case = next((row for row in pack.get("cases", [])
                     if row.get("task") == task and not scenario_rt.participant_hidden(row)), None)
        existing = _last_snap()
        if existing and existing.get("scenario_id") == sk["scenario_id"] and \
                not any(row.get("sid") == existing["id"] for row in mine(STATE["runs"])) and \
                (case is None or existing.get("case_id") == case.get("id")):
            snap = existing
        elif case:
            snap = scenario_rt.case_snapshot(pack, case["id"])
            snap.update({"id": nid("s"), "skill": STATE["active"], "recorded": time.time()})
            with LOCK:
                STATE["snapshots"][snap["id"]] = snap
        else:
            return {"error": "当前研究工作区只执行已经冻结的历史情况和发布前检查"}
        ins, _ = instructions_for({})
        acc = execute_batch(ins, snap, {}, k)
        su = summarize(acc)
        ok = [row for row in acc if "error" not in row]
        return {"snapshot": snap["id"], "summary": su,
                "workflow": ok[0].get("steps") if ok else [],
                "outcome": ok[0].get("outcome") if ok else "", "missing": []}
    srcs = sk.get("sources") or {}
    tools, args, missing = {}, {}, []
    ordered = sorted(srcs.items(),
                     key=lambda kv: {"data": 0, "query": 1, "write": 2}.get(kv[1].get("kind"), 1))
    for name, src in ordered:
        kind = src.get("kind")
        if kind == "write":
            continue
        if kind == "data":
            if src.get("rows"):
                tools[name] = src["rows"]
            else:
                missing.append(name)
            continue
        owned = {k2: v for k2, v in tools.items()
                 if (srcs.get(k2) or {}).get("kind") == "data"}
        lines = "\n".join("%d. %s" % (i["n"], i["text"]) for i in sk["instructions"])
        fxs = src.get("fixture") or []
        sample = ("\n\nSAMPLE RECORDS FROM THIS SERVICE:\n"
                  + json.dumps(fxs[:3], ensure_ascii=False)) if fxs else ""
        qa = ask(P_ARGS, "TOOL: %s%s -> %s%s\n\nUSER DATA:\n%s\n\nTASK: %s" % (
            name, src.get("signature") or "", src.get("returns") or "", sample,
            json.dumps(owned, ensure_ascii=False)[:2500], task), 900)
        a_ = (qa or {}).get("args") or {}
        args[name] = a_
        fx = src.get("fixture")
        if fx:
            got = query_fixture(fx, a_)      # 后台固定表：确定性查询，不调模型
        else:
            res = ask(P_CONNECT, "SERVICE: %s%s -> %s\n\nPARAMETERS: %s" % (
                name, src.get("signature") or "", src.get("returns") or "",
                json.dumps(a_, ensure_ascii=False)), 3000)
            got = res.get("result") if res else None
        if got is None or (isinstance(got, (list, dict)) and len(got) == 0):
            missing.append(name)
        else:
            tools[name] = got
    if not tools:
        return {"error": "没有可用的数据源", "missing": missing}
    snap = {"id": nid("s"), "task": task, "tools": tools, "args": args, "missing": missing,
            "summary": "", "fact_schema": None, "skill": STATE["active"], "recorded": time.time()}
    fs = ask(P_FACTS, "TASK: %s\nTOOLS: %s" % (task, ", ".join(tools)), 900)
    if fs and "keys" in fs:
        snap["fact_schema"] = fs["keys"]
    with LOCK:
        STATE["snapshots"][snap["id"]] = snap
    ins, _ = instructions_for({})
    acc = execute_batch(ins, snap, {}, k)
    su = summarize(acc)
    ok = [r for r in acc if "error" not in r]
    return {"snapshot": snap["id"], "summary": su,
            "workflow": ok[0].get("steps") if ok else [],
            "outcome": ok[0].get("outcome") if ok else "",
            "missing": missing}


def op_suggest_cases():
    snap = _last_snap()
    sk = cur()
    if not snap:
        return {"error": "还没有执行过任务"}
    if sk.get("scenario_id"):
        snap = _incident_snap() or snap
    if sk.get("scenario_id") and snap.get("scenario_id") == sk["scenario_id"]:
        plan = ensure_scope_plan(sk)
        fallback_cases = scenario_rt.neighbouring_cases(
            scenario_rt.get_pack(sk["scenario_id"]),
            scenario_rt.get_pack(sk["scenario_id"])["entry_case"])
        return {"cases": copy.deepcopy((plan or {}).get("cases") or fallback_cases),
                "intent": copy.deepcopy((plan or {}).get("intent") or {}),
                "plan_id": (plan or {}).get("id"),
                "source": (plan or {}).get("source") or "frozen-case-bank"}
    out = ask(P_CONTRAST, "SKILL:\n%s\n\nWHAT MAY CHANGE: %s" % (
        "\n".join("%d. %s" % (i["n"], i["text"]) for i in sk["instructions"]),
        "；".join(row["commitment"] for row in mine(STATE["situations"])[-3:])), 1500)
    return {"cases": (out or {}).get("situations", []), "source": "generated"}


def op_open_case(case_id, k=BASELINE_RUNS):
    sk = cur()
    if not sk.get("scenario_id"):
        return {"error": "开放 skill 的相关情况需要在结构化工作区确认后生成"}
    pack = scenario_rt.get_pack(sk["scenario_id"])
    allowed = {item["case_id"] for case in pack.get("cases", [])
               if not scenario_rt.participant_hidden(case)
               for item in case.get("neighbours", [])}
    if case_id not in allowed:
        return {"error": "该情况不是当前可见的发布前检查"}
    snap = scenario_rt.case_snapshot(pack, case_id)
    snap.update({"id": nid("s"), "skill": STATE["active"], "recorded": time.time(),
                 "parent_snapshot": (_last_snap() or {}).get("id")})
    with LOCK:
        STATE["snapshots"][snap["id"]] = snap
    ins, _ = instructions_for({})
    runs = execute_batch(ins, snap, {}, k)
    summary = summarize(runs)
    ok = [row for row in runs if "error" not in row]
    return {"case_id": case_id, "snapshot": snap["id"], "task": snap["task"],
            "summary": summary, "workflow": ok[0].get("steps") if ok else [],
            "outcome": ok[0].get("outcome") if ok else ""}


def _last_snap():
    xs = [x for x in STATE["snapshots"].values() if x.get("skill") == STATE["active"]]
    return xs[-1] if xs else None


def _incident_snap():
    """Return the active repair round's incident after neighbouring cases are opened."""
    xs = [x for x in STATE["snapshots"].values()
          if x.get("skill") == STATE["active"] and x.get("case_role") == "incident"]
    return xs[-1] if xs else None


def _snapshot_for_case(case_id):
    """Return the newest active snapshot for one explicit scenario case."""
    xs = [row for row in STATE["snapshots"].values()
          if row.get("skill") == STATE["active"] and row.get("case_id") == case_id]
    return xs[-1] if xs else None


def _baseline_case_runs(sk, snap):
    if not snap:
        return []
    return [row for row in mine(STATE["runs"])
            if row.get("sid") == snap.get("id") and not (row.get("variant") or {})
            and "error" not in row and
            (not row.get("artifact_hash") or row.get("artifact_hash") == sk.get("content_hash"))]


def _compact_case_outcome(sk, facts, fallback="", case=None):
    """Translate execution facts into one scannable sentence for the chat condition."""
    facts = facts or {}
    domain = ""
    if sk.get("scenario_id"):
        domain = scenario_rt.get_pack(sk["scenario_id"]).get("domain") or ""
    if domain == "travel" and facts:
        parts = []
        price_number = _num(facts.get("price"))
        if facts.get("selected_flight"):
            parts.append("选择 %s" % facts["selected_flight"])
        if facts.get("price") is not None:
            parts.append("净增费用 $%s" % facts["price"])
        has_fixed_commitment = bool(
            ((case or {}).get("world") or {}).get("state", {}).get("required_arrival_before"))
        if has_fixed_commitment and facts.get("on_time_for_commitment") is True:
            parts.append("能在固定承诺前抵达")
        elif has_fixed_commitment and facts.get("on_time_for_commitment") is False:
            parts.append("无法在固定承诺前抵达")
        if facts.get("confirmation_before_booking") is True:
            parts.append("出票前已确认")
        elif price_number is not None and price_number > 500:
            parts.append("出票前未确认")
        if facts.get("booking_completed") is True:
            parts.append("已完成改签")
        elif facts.get("booking_completed") is False:
            parts.append("未完成改签")
        if parts:
            return "；".join(parts) + "。"
    if domain == "expense" and facts:
        decisions = {"approve": "通过", "reject": "退回", "escalate": "升级审批"}
        parts = []
        if facts.get("decision") is not None:
            parts.append("记录为%s" % decisions.get(facts["decision"], facts["decision"]))
        if facts.get("manager_approval_requested") is True:
            parts.append("已请求主管审批")
        elif facts.get("restricted_category") is True:
            parts.append("未请求主管审批")
        if facts.get("ledger_entry_created") is True:
            parts.append("已入账")
        elif facts.get("ledger_entry_created") is False:
            parts.append("未入账")
        if parts:
            return "；".join(parts) + "。"
    fallback = (fallback or "").strip().replace("\n", " ")
    return fallback[:220] + ("…" if len(fallback) > 220 else "")


def _case_review_context(sk, case_id):
    """Build participant-facing case evidence without oracle or hidden holdout data."""
    pack = scenario_rt.get_pack(sk["scenario_id"])
    case = scenario_rt.get_case(pack, case_id)
    if scenario_rt.participant_hidden(case):
        raise ValueError("该情况不在本次产品评审范围内")
    snap = _snapshot_for_case(case_id)
    runs = _baseline_case_runs(sk, snap)
    summary = summarize(runs) if runs else {}
    group = (summary.get("groups") or [{}])[0]
    prompt = case.get("review_prompt") or {}
    proposal = None
    if prompt.get("baseline_commitment"):
        proposal = {"disposition": "preserve",
                    "commitment": prompt["baseline_commitment"]}
    elif prompt.get("unresolved_commitment"):
        proposal = {"disposition": "unresolved",
                    "commitment": prompt["unresolved_commitment"]}
    return {
        "case_id": case["id"],
        "snapshot_id": (snap or {}).get("id"),
        "summary": case.get("summary") or case["id"],
        "task": case.get("task") or "",
        "changed_factors": copy.deepcopy(case.get("changed_factors") or []),
        "outcome": _compact_case_outcome(sk, group.get("facts") or {},
                                          group.get("outcome") or "", case),
        "facts": copy.deepcopy(group.get("facts") or {}),
        "run_count": len(runs),
        "proposal": proposal,
        "question": prompt.get("boundary_question") or "",
    }


def related_case_contexts(sk, only_case_ids=None):
    if not sk or not sk.get("scenario_id"):
        return []
    pack = scenario_rt.get_pack(sk["scenario_id"])
    entry = scenario_rt.get_case(pack, pack["entry_case"])
    plan = current_scope_plan(sk)
    planned = (plan or {}).get("cases") or []
    allowed = ([row["case_id"] for row in planned] if planned else
               [row["case_id"] for row in entry.get("neighbours", [])])
    plan_by_id = {row["case_id"]: row for row in planned}
    wanted = set(only_case_ids or allowed)
    contexts = []
    for case_id in allowed:
        if case_id not in wanted:
            continue
        context = _case_review_context(sk, case_id)
        relation = plan_by_id.get(case_id) or {}
        context.update({
            "text": context.get("summary"),
            "suggest": relation.get("suggest"),
            "exposure": relation.get("exposure", "author-visible"),
            "why": relation.get("why"),
            "relation_type": relation.get("relation_type"),
            "why_relevant": relation.get("why_relevant") or relation.get("intent_link") or
                            relation.get("why"),
            "owner_question": relation.get("owner_question"),
            "review_prompt": copy.deepcopy(
                scenario_rt.get_case(pack, case_id).get("review_prompt") or {}),
        })
        contexts.append(context)
    return contexts


def op_prepare_related_cases(k=BASELINE_RUNS, before_case=None, commitment=None):
    """Execute all product-visible neighbouring cases without user-facing tool commands."""
    sk = cur()
    if not sk or not sk.get("scenario_id"):
        return {"error": "当前 skill 没有冻结的相关情况"}
    pack = scenario_rt.get_pack(sk["scenario_id"])
    plan = ensure_scope_plan(sk, commitment)
    if not plan:
        return {"error": "还没有明确本次修复目标"}
    incident = _incident_snap()
    prepared = []
    pending = []
    instructions, _ = instructions_for({})
    for item in plan.get("cases") or []:
        case_id = item["case_id"]
        case = scenario_rt.get_case(pack, case_id)
        if scenario_rt.participant_hidden(case):
            continue
        snap = _snapshot_for_case(case_id)
        runs = _baseline_case_runs(sk, snap)
        if not runs:
            if before_case:
                before_case(case)
            if not snap:
                snap = scenario_rt.case_snapshot(pack, case_id)
                snap.update({"id": nid("s"), "skill": STATE["active"],
                             "recorded": time.time(),
                             "parent_snapshot": (incident or {}).get("id")})
                with LOCK:
                    STATE["snapshots"][snap["id"]] = snap
            pending.append(snap)
        prepared.append(case_id)
    if pending:
        # Cases have independent frozen worlds. Running them concurrently keeps
        # the same per-case repetition budget while avoiding three sequential
        # model-call waves in the conversational path.
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(pending))) as executor:
            list(executor.map(lambda snap: execute_batch(instructions, snap, {}, k), pending))
    return {"prepared": prepared, "cases": related_case_contexts(sk),
            "intent": copy.deepcopy(plan.get("intent") or {}),
            "plan_id": plan.get("id"), "plan_hash": plan.get("hash"),
            "source": plan.get("source")}


def extract_scope_decisions(message, cases):
    """Parse one natural reply into zero or more explicit, case-bound decisions."""
    public_cases = [{
        "index": index + 1,
        "case_id": row["case_id"],
        "summary": row.get("summary"),
        "current_outcome": row.get("outcome"),
        "proposal": copy.deepcopy(row.get("proposal")),
        "open_question": row.get("question"),
    } for index, row in enumerate(cases)]
    parsed = ask(P_SCOPE_PARSE, "CASES:\n%s\n\nOWNER REPLY:\n%s" % (
        json.dumps(public_cases, ensure_ascii=False), message), 1800) or {}
    by_id = {row["case_id"]: row for row in cases}
    decisions = []
    seen = set()
    for row in parsed.get("decisions") or []:
        case_id = row.get("case_id")
        disposition = row.get("disposition")
        commitment = (row.get("commitment") or "").strip()
        if case_id not in by_id or case_id in seen or \
                disposition not in ("change", "preserve", "unresolved", "excluded") or not commitment:
            continue
        decisions.append({"case_id": case_id, "disposition": disposition,
                          "commitment": commitment})
        seen.add(case_id)
    if parsed.get("accept_proposals") is True:
        for case in cases:
            proposal = case.get("proposal") or {}
            if case["case_id"] in seen or not proposal.get("commitment"):
                continue
            decisions.append({"case_id": case["case_id"],
                              "disposition": proposal["disposition"],
                              "commitment": proposal["commitment"]})
            seen.add(case["case_id"])
    return {
        "understood": bool(decisions) and parsed.get("understood") is not False,
        "decisions": decisions,
        "clarification": (parsed.get("clarification") or "").strip(),
        "accepted_proposals": parsed.get("accept_proposals") is True,
    }


def related_review_text(cases, prefix="", intent=None):
    """Render an ephemeral conversational review, not a persistent case panel."""
    lines = []
    if prefix:
        lines.append(prefix)
    intent = intent or {}
    if intent.get("summary"):
        lines.append("我把你的处理原则理解为：**%s**" % intent["summary"])
    if intent.get("ambiguities"):
        lines.append("仍需确认：\n" + "\n".join("- " + value
                                               for value in intent["ambiguities"]))
    lines.append("我围绕这条规则检查了 %d 种情况：" % len(cases))
    relation_labels = {
        "outside-trigger": "适用范围反例",
        "existing-safeguard": "既有审核保护",
        "definition-boundary": "规则定义边界",
        "policy-limit": "规则适用上限",
        "action-boundary": "处理流程边界",
        "related-case": "相关情况",
    }
    for index, case in enumerate(cases, 1):
        relation = relation_labels.get(case.get("relation_type"), "相关情况")
        lines.append("**%d. %s · %s**" % (index, relation, case["summary"]))
        if case.get("why_relevant"):
            lines.append("与这条规则的关系：" + case["why_relevant"])
        if case.get("changed_factors"):
            lines.append("变化条件：" + "；".join(case["changed_factors"]))
        if case.get("outcome"):
            lines.append("原版本执行结果：" + case["outcome"])
        proposal = case.get("proposal") or {}
        if proposal.get("disposition") == "preserve":
            lines.append("我的建议：保留“%s”。" %
                         proposal["commitment"].rstrip("。.!！"))
        elif case.get("owner_question") or case.get("question"):
            lines.append("需要你明确：" + (case.get("owner_question") or case["question"]))
    lines.append("请确认这些建议，并回答仍未明确的问题；也可以修改任一项，或明确说明某项与本次修复无关。")
    return "\n\n".join(lines)


def repair_preview_text(sk, preview=None):
    """Explain a source-location cue without turning it into a policy gate."""
    public = public_repair_preview(sk, preview)
    if not public:
        return "系统暂时无法确定相关的原规则位置。"
    assessment = public.get("assessment") or {}
    lines = ["**相关修改依据**",
             "原 Skill 的第 %s 条与当前问题最相关：%s" % (
                 public.get("instruction"), public.get("instruction_text") or "")]
    if public.get("case_summary"):
        lines.append("**为什么建议这里** 在“%s”中，%s" % (
            public["case_summary"], assessment.get("summary") or
            "这条规则与当前问题的处理过程有关。"))
    else:
        lines.append("**为什么建议这里** " + (assessment.get("summary") or
                     "这条规则与当前问题的处理过程有关。"))
    lines.append(public.get("limitation") or
                 "候选生成后，系统还会在所有已确认情况中重新检查。")
    lines.append("这是一项可回看的定位参考，不需要单独确认；候选仍以你已经确认的适用范围为准。")
    return "\n\n".join(lines)


def recorded_scope_text(decisions):
    labels = {"change": "需要改变", "preserve": "保持现有行为",
              "unresolved": "保留人工判断", "excluded": "不纳入本轮修复范围"}
    lines = ["我已经按具体情况记录了你的判断："]
    for row in decisions:
        lines.append("- %s：%s" % (labels[row["disposition"]], row["commitment"]))
    return "\n".join(lines)


def op_probe(kind, n=None, k=SOURCE_PROBE_RUNS):
    snap = _last_snap()
    sk = cur()
    if not snap:
        return {"error": "还没有执行过任务"}
    base = [r for r in mine(STATE["runs"]) if r.get("sid") == snap["id"]
            and not r.get("variant") and "error" not in r
            and r.get("artifact_hash") == sk.get("content_hash")]
    out = {}
    kinds = ("delete", "invert") if kind == "instruction" else ("perturb",)
    for kd in kinds:
        if kd == "delete":
            variant = {"mask": [n]}
        elif kd == "invert":
            src = [i for i in sk["instructions"] if i["n"] == n]
            inv = ask(P_INVERT, src[0]["text"], 900) if src else None
            if not inv or "text" not in inv:
                continue
            variant = {"rewrite": {"n": n, "text": inv["text"]}}
        else:
            pt = ask(P_PERTURB, "TASK: %s\n\nTOOL RESULTS:\n%s" % (
                snap["task"], json.dumps(snap["tools"], ensure_ascii=False)[:4000]), 1500)
            if not pt or "tool" not in pt:
                continue
            variant = {"perturb": pt}
        ins, _ = instructions_for(variant)
        acc = execute_batch(ins, snap, variant, k)
        dk = discriminating(base, acc)
        bm, bs = modal(sig_by(r, dk) for r in base)
        m, sh = modal(sig_by(r, dk) for r in acc)
        out[kd] = {"changed": (bm != m) if (bm and m) else None, "share": sh}
        probe = {"id": nid("p"), "skill": STATE["active"], "kind": kd, "n": n,
                 "note": "", "sid": snap["id"],
                 "changed": out[kd]["changed"], "confidence":
                 "ok" if bs >= .6 else "unstable",
                 "base_share": round(bs, 2), "probe_share": round(sh or 0, 2),
                 "k": len(acc)}
        probe.update(intervention_provenance(
            sk, snap, variant, ins, base, acc, "source_location"))
        with LOCK:
            STATE["probes"].append(probe)
    if kind == "instruction":
        d, i = out.get("delete"), out.get("invert")
        if bs < .6 or not d or not i or d["changed"] is None or i["changed"] is None:
            code = "unsure"
        elif not d["changed"] and not i["changed"]:
            code = "dead"
        else:
            code = "controls"
        return {"instruction": n, "verdict": code, "base_share": round(bs, 2)}
    p = out.get("perturb")
    return {"verdict": ("unused" if p and p["changed"] is False else
                        "used" if p and p["changed"] else "unsure")}


def op_block_probe(n, case_id=None, k=BLOCK_PROBE_RUNS):
    """Run the chat/workspace-equivalent Mp check against a committed candidate."""
    sk = cur()
    if not sk or not sk.get("candidate"):
        return {"error": "需要先有候选版本"}
    try:
        n = int(n)
    except (TypeError, ValueError):
        return {"error": "需要明确的候选指令编号"}
    if not any(row.get("n") == n for row in sk["candidate"].get("instructions") or []):
        return {"error": "候选指令 %d 不存在" % n}
    if not case_id:
        problematic = next((row for row in sk.get("last_compare") or []
                            if row.get("verdict") in ("unmet", "broken", "needs_judgment")), None)
        case_id = (problematic or {}).get("case_id")
    snap = _snapshot_for_case(case_id) if case_id else _last_snap()
    if not snap:
        return {"error": "找不到要检查的情况"}
    k = BLOCK_PROBE_RUNS
    base = [row for row in mine(STATE["runs"])
            if row.get("sid") == snap["id"] and (row.get("variant") or {}).get("draft")
            and not (row.get("variant") or {}).get("mask")
            and not (row.get("variant") or {}).get("rewrite")
            and "error" not in row
            and row.get("artifact_hash") == sk["candidate"].get("content_hash")]
    base = base[-BLOCK_PROBE_RUNS:]
    if not base:
        instructions, _ = instructions_for({"draft": True})
        base = execute_batch(instructions, snap, {"draft": True}, MATCHED_RUNS)
    variant = {"draft": True, "mask": [n]}
    instructions, _ = instructions_for(variant)
    runs = execute_batch(instructions, snap, variant, k)
    keys = discriminating(base, runs)
    changed, changed_fields, weak = compare_fields(base, runs, keys)
    code = "unsure" if changed is None or weak else "responsible" if changed else "elsewhere"
    rec = {"id": nid("p"), "skill": sk["id"], "kind": "block", "n": n,
           "note": "临时移除候选指令 %d" % n, "sid": snap["id"],
           "case_id": snap.get("case_id"), "case_summary": snap.get("summary"),
           "changed": changed, "changed_fields": changed_fields,
           "confidence": "unstable" if code == "unsure" else "ok", "k": len(runs)}
    rec.update(intervention_provenance(
        sk, snap, variant, instructions, base, runs, "candidate_block"))
    with LOCK:
        STATE["probes"].append(rec)
    return {"code": code, "probe": copy.deepcopy(rec),
            "interpretation": {
                "responsible": "不采用这项修改时，当前情况的可观察结果发生变化；该修改块与结果有关。",
                "elsewhere": "不采用这项修改时，未观察到稳定变化；当前结果可能来自其他候选内容。",
                "unsure": "候选基线或干预结果不够稳定，暂时不能判断。",
            }[code],
            "limitation": "这是候选块敏感性检查，不证明唯一因果责任。"}


def op_expect(text, disposition, case_id=None):
    """Record one explicit case-bound policy decision, replacing duplicates safely."""
    snap = _snapshot_for_case(case_id) if case_id else _last_snap()
    if not snap:
        return {"error": "这个情况还没有完成执行，暂时不能记录判断"}
    sk = cur()
    if sk.get("candidate"):
        return {"error": "已存在草稿，判定标准在本轮评审中锁定"}
    disposition = (disposition or "").strip().lower()
    if disposition not in ("change", "preserve", "unresolved", "excluded"):
        return {"error": "判断类型无效"}
    text = (text or "").strip()
    if not text:
        return {"error": "判断内容为空"}
    if disposition == "excluded" and snap.get("case_role") == "incident":
        return {"error": "问题场景必须保留一项明确的修复目标"}
    runs = [r for r in mine(STATE["runs"]) if r.get("sid") == snap["id"]
            and "error" not in r and not r.get("variant")]
    keys = sorted({k for r in runs for k in (r.get("facts") or {})})
    kinds = sorted({s.get("kind") for r in runs for s in r.get("steps", []) if s.get("kind")})
    crit = None
    if disposition in ("change", "preserve"):
        scenario_criterion = None
        if disposition == "preserve":
            scenario_criterion = (snap.get("review_prompt") or {}).get("baseline_criterion")
        elif disposition == "change" and sk.get("scenario_id") and \
                snap.get("case_role") == "incident" and runs:
            # The incident suggestion is generated from this completed trace
            # and is already participant-facing. Reuse its observable fact
            # criterion instead of asking another model call to invent a proxy
            # such as tool-call order that may not measure the requested result.
            issue = scenario_rt.analyze_issue(
                scenario_rt.get_pack(sk["scenario_id"]), snap, runs[-1]) or {}
            suggestion = next((row for row in issue.get("suggestions") or []
                               if row.get("criterion")), None)
            scenario_criterion = (suggestion or {}).get("criterion")
        if scenario_criterion:
            crit = copy.deepcopy(scenario_criterion)
            crit["trial"] = [eval_criterion(crit, r) for r in runs[-3:]]
        else:
            c = ask(P_CRIT, "EXPECTATION: %s\n\nFACT KEYS: %s\n\nSTEP KINDS: %s\n\nSAMPLE:\n%s" % (
                text, keys, kinds,
                json.dumps(runs[-1], ensure_ascii=False)[:1500] if runs else "{}"), 2500)
            cands = (c or {}).get("candidates") or []
            for x in cands:
                x["trial"] = [eval_criterion(x, r) for r in runs[-3:]]
            good = [x for x in cands if x["trial"] and any(v is not None for v in x["trial"])]
            crit = (good or cands or [None])[0]
    now = time.time()
    sealed = snap.get("case_role") == "generator-withheld"
    st = {"id": nid("t"), "skill": STATE["active"], "sid": snap["id"], "commitment": text,
          "criterion": crit, "disposition": disposition, "label": (crit or {}).get("label", ""),
          "sealed": sealed, "case_source": snap.get("source") or "user",
          "case_context": snap.get("task", ""), "case_id": snap.get("case_id"),
          "case_hash": snap.get("case_hash"),
          "pre_reveal": not bool(sk.get("first_candidate_revealed_at")),
          "review_round": sk.get("review_round") or 1,
          "judged_before_candidate_in_round": True,
          "post_reveal": bool(sk.get("first_candidate_revealed_at")),
          "judged_at": now, "owner_revealed_at": now,
          "generator_exposure": "withheld" if sealed else "visible",
          "candidate_outcome_revealed_at": None, "created": now}
    target_case_id = snap.get("case_id")
    replaced = False
    with LOCK:
        sk["scope_version"] = (sk.get("scope_version") or 1) + 1
        st["scope_version"] = sk["scope_version"]
        matches = [row for row in STATE["situations"]
                   if row.get("skill") == STATE["active"] and
                   (row.get("case_id") or
                    (STATE["snapshots"].get(row.get("sid")) or {}).get("case_id")) ==
                   target_case_id]
        if matches:
            # Scope history already preserves the previous value. Keep one live
            # record per case so a conversational correction cannot silently
            # create contradictory duplicate judgments.
            keep = matches[-1]
            st["id"] = keep.get("id") or st["id"]
            st["created"] = keep.get("created") or st["created"]
            STATE["situations"] = [row for row in STATE["situations"]
                                   if row not in matches]
            replaced = True
        STATE["situations"].append(st)
        sk["scope_revision_required"] = False
        sk["active_repair_preview_id"] = None
        scope_record = freeze_scope_version(sk)
    response = {"recorded": text, "disposition": disposition,
            "criterion": (crit or {}).get("label"), "scope_version": sk["scope_version"],
            "scope_hash": scope_record["hash"], "case_id": target_case_id,
            "case_summary": snap.get("summary"), "replaced": replaced}
    if disposition == "change" and sk.get("scenario_id") and snap.get("case_role") == "incident":
        plan = ensure_scope_plan(sk, text)
        response["related_cases"] = copy.deepcopy((plan or {}).get("cases") or [])
        response["scope_plan"] = {"id": (plan or {}).get("id"),
                                  "hash": (plan or {}).get("hash"),
                                  "intent": copy.deepcopy((plan or {}).get("intent") or {})}
        record_semantic_event("intent_committed", sk, {
            "case_id": target_case_id, "scope_version": sk.get("scope_version") or 1,
            "scope_hash": scope_record["hash"],
        })
    if scope_readiness(sk).get("ready"):
        record_semantic_event("scope_committed", sk, {
            "scope_version": sk.get("scope_version") or 1,
            "scope_hash": scope_record["hash"],
            "item_count": len(scope_items(sk["id"])),
        })
    return response


def op_edit_instruction(n, text):
    sk = cur()
    if not sk:
        return {"error": "尚未导入 skill"}
    try:
        n = int(n)
    except (TypeError, ValueError):
        return {"error": "需要明确的指令编号"}
    text = (text or "").strip()
    if not text:
        return {"error": "新指令内容为空"}
    source = (sk.get("candidate") or sk).get("instructions") or []
    if not any(row.get("n") == n for row in source):
        return {"error": "指令 %d 不存在" % n}
    edited = [{"n": row.get("n"), "text": text if row.get("n") == n else row.get("text")}
              for row in source]
    try:
        candidate = save_owner_candidate(sk, edited, "chat-owner")
    except ValueError as error:
        return {"error": str(error)}
    return {"edited": True, "instruction": n, "text": text,
            "candidate_hash": candidate["hash"], "author": candidate["author"]}


def op_compare(k=MATCHED_RUNS):
    sits = scope_items()
    if not sits:
        return {"error": "还没有记录任何预期"}

    def compare_situation(x):
        snap = STATE["snapshots"].get(x.get("sid"))
        if not snap:
            return None
        res = {}
        for w in ("base", "draft"):
            variant = {"draft": True} if w == "draft" else {}
            ins, _ = instructions_for(variant)
            acc = execute_batch(ins, snap, variant, k, x.get("criterion"))
            su = summarize(acc, x.get("criterion"))
            res[w] = su
        b, a = res["base"], res["draft"]
        moved = (b["top"] != a["top"])
        if b["share"] < .6 or a["share"] < .6:
            verdict = "insufficient"
        elif x["disposition"] == "change":
            verdict = "met" if (a["pass"] > a["tot"] / 2 if a["tot"] else moved) else "unmet"
        elif x["disposition"] == "preserve":
            if b["tot"] and a["tot"]:
                baseline_met = b["pass"] > b["tot"] / 2
                candidate_met = a["pass"] > a["tot"] / 2
                broken = baseline_met and not candidate_met
            else:
                broken = moved
            verdict = "broken" if broken else "kept"
        else:
            verdict = "needs_judgment" if moved else "untouched"
        if not x.get("candidate_outcome_revealed_at"):
            x["candidate_outcome_revealed_at"] = time.time()
        return {"expectation": x["commitment"], "disposition": x["disposition"],
                "verdict": verdict, "situation_id": x.get("id"),
                "case_id": snap.get("case_id"), "case_context": x.get("case_context")}

    # Each situation has an isolated world and its own persisted run records.
    # Parallelizing across situations changes latency, not execution count or
    # evidence semantics. Limit outer concurrency to avoid an API burst.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(sits))) as executor:
        rows = [row for row in executor.map(compare_situation, sits) if row]
    sk = cur()
    if sk is not None:
        if not sk.get("first_candidate_revealed_at"):
            sk["first_candidate_revealed_at"] = time.time()
        sk["last_compare"] = copy.deepcopy(rows)
        sk["last_compare_budget"] = k
    return {"rows": rows}


def candidate_review_text(sk, comparison):
    """Summarize the candidate and matched evidence as an ordinary chat turn."""
    candidate = sk.get("candidate") or {}
    old = {row.get("n"): row.get("text") for row in sk.get("instructions") or []}
    changed = [row for row in candidate.get("instructions") or []
               if old.get(row.get("n")) != row.get("text")]
    lines = ["候选 v%s 已经生成，并在你确认的全部情况上完成了修改前后检查。" %
             (candidate.get("version") or "—")]
    if changed:
        lines.append("**候选修改** 共调整 %d 条指令：" % len(changed))
        for row in changed[:3]:
            lines.append("- 指令 %s：%s" % (row.get("n"), row.get("text")))
        if len(changed) > 3:
            lines.append("其余 %d 条可在会话中附带的“完整候选 Skill”里展开查看。" %
                         (len(changed) - 3))
    verdicts = {"met": "达到预期", "unmet": "没有达到预期",
                "kept": "原有行为得到保留", "broken": "原有行为被破坏",
                "needs_judgment": "行为发生变化，需要你判断",
                "untouched": "未观察到变化", "insufficient": "证据不足"}
    contexts = {row["case_id"]: row for row in
                ([_case_review_context(sk, scenario_rt.get_pack(sk["scenario_id"])["entry_case"])]
                 + related_case_contexts(sk))} if sk.get("scenario_id") else {}
    rows = comparison.get("rows") or []
    if rows:
        lines.append("**执行检查**")
        for row in rows:
            title = (contexts.get(row.get("case_id")) or {}).get("summary") or \
                    row.get("case_context") or "已记录情况"
            lines.append("- %s：%s" % (title, verdicts.get(row.get("verdict"), row.get("verdict"))))
    conflicts = [row for row in rows if row.get("verdict") in ("unmet", "broken")]
    judgments = [row for row in rows if row.get("verdict") == "needs_judgment"]
    insufficient = [row for row in rows if row.get("verdict") == "insufficient"]
    if conflicts:
        lines.append("候选有 %d 项与你确认的目标或保留要求冲突，建议继续调整后再发布。" %
                     len(conflicts))
        if judgments:
            lines.append("另外有 %d 个未决边界被改变，需要你明确判断。" % len(judgments))
        if insufficient:
            lines.append("另有 %d 项执行证据不足，需要重新检查。" % len(insufficient))
        lines.append("希望如何调整？如仍要发布，请说明理由。")
    elif insufficient:
        lines.append("有 %d 项执行证据不足，建议重新检查后再决定是否发布。" % len(insufficient))
        lines.append("你可以让我重新检查，或者暂缓本次发布。")
    elif judgments:
        lines.append("有 %d 个未决边界被候选改变，需要你决定是否接受。" % len(judgments))
        lines.append("希望继续调整，还是按当前版本发布？")
    else:
        lines.append("没有发现与你已确认判断相冲突的结果。是否发布当前候选版本？")
    return "\n\n".join(lines)

# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, code, ctype, payload):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode())

    def _body(self):
        if hasattr(self, "_cached_body"):
            return self._cached_body
        n = int(self.headers.get("Content-Length", 0))
        self._cached_body = json.loads(self.rfile.read(n) or b"{}")
        return self._cached_body

    def _open(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _chunk(self, obj):
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        self.wfile.write(("%X\r\n" % len(line)).encode() + line + b"\r\n")
        self.wfile.flush()

    def _close(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/chat", "/chat.html"):
            f = HERE / "chat.html"
            return self._send(200, "text/html; charset=utf-8", f.read_bytes()) if f.exists() \
                else self._send(404, "text/plain", b"chat.html missing")
        if path in ("/questionnaire", "/questionnaire.html"):
            f = HERE / "questionnaire.html"
            return self._send(200, "text/html; charset=utf-8", f.read_bytes()) if f.exists() \
                else self._send(404, "text/plain", b"questionnaire.html missing")
        if path in ("/", "/index.html", "/app.html"):
            f = HERE / "app.html"
            return self._send(200, "text/html; charset=utf-8", f.read_bytes()) if f.exists() \
                else self._send(404, "text/plain", b"app.html missing")
        if path == "/api/scenarios":
            return self._json({"scenarios": scenario_rt.public_scenarios()})
        if path == "/api/chat/bootstrap":
            session = (urllib.parse.parse_qs(parsed.query).get("session") or [""])[0][:80]
            return self._json(public_chat_bootstrap(session))
        if path == "/api/state":
            active_runs = mine(STATE["runs"])
            public_runs = [{key: value for key, value in row.items() if key != "_oracle"}
                           for row in active_runs]
            base_by_snapshot = {}
            for row in active_runs:
                if not (row.get("variant") or {}) and "error" not in row:
                    base_by_snapshot.setdefault(row.get("sid"), []).append(row)
            return self._json({
                "skills": [{"id": k, "name": v["name"], "version": v["version"],
                            "hash": v["hash"], "n": len(v["instructions"]),
                            "draft": bool(v.get("candidate")),
                            "scenario_id": v.get("scenario_id")}
                           for k, v in STATE["skills"].items()],
                "active": STATE["active"],
                "skill": cur(), "candidate": (cur() or {}).get("candidate"),
                "work_order": copy.deepcopy((cur() or {}).get("work_order")),
                "study_context": copy.deepcopy((cur() or {}).get("study_context") or {}),
                "scope_version": (cur() or {}).get("scope_version") or 1,
                "sources": [{"tool": k, "label": v.get("label") or k, "kind": v.get("kind"),
                             "signature": v.get("signature"), "returns": v.get("returns"),
                             "rows": v.get("rows") or [], "n": len(v.get("rows") or [])}
                            for k, v in ((cur() or {}).get("sources") or {}).items()],
                "versions": len((cur() or {}).get("versions") or []),
                "snapshots": [x for x in STATE["snapshots"].values()
                              if x.get("skill") == STATE["active"]],
                "runs": public_runs,
                "summaries": {sid: summarize(rows) for sid, rows in base_by_snapshot.items()},
                "situations": scope_items(), "probes": mine(STATE["probes"]),
                "scope_plan": current_scope_plan(cur()),
                "repair_preview": public_repair_preview(cur()),
                "review_round": (cur() or {}).get("review_round") or 1,
                "scope_revision_required": bool((cur() or {}).get("scope_revision_required")),
                "reviews": mine(STATE.get("reviews") or []),
                "chat": mine(STATE.get("chat") or []),
                "history": [{"version": v.get("version"), "hash": v.get("hash"),
                             "n": len(v.get("instructions") or [])}
                            for v in ((cur() or {}).get("versions") or [])],
                "model": MODEL, "keyed": bool(API_KEY),
                "review_temperature": REVIEW_TEMPERATURE,
                "agent_temperature": AGENT_TEMPERATURE,
                "runtime": (cur() or {}).get("config", {}).get("execution_runtime")})
        return self._send(404, "text/plain", b"not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        fn = getattr(self, "h_" + path.strip("/").replace("/", "_"), None)
        if fn is None:
            return self._send(404, "text/plain", b"not found")
        # BaseHTTPRequestHandler reuses one Handler instance for HTTP/1.1
        # keep-alive requests. A body cache must therefore be request-scoped.
        if hasattr(self, "_cached_body"):
            del self._cached_body
        self._body()  # consume and cache every HTTP/1.1 request body, including bodyless handlers
        if not API_KEY and path not in ("/api/event", "/api/scenario/load"):
            return self._json({"error": "DEEPSEEK_API_KEY 未设置"}, 500)
        try:
            result = fn()
            if path != "/api/study/questionnaire/submit":
                try:
                    archive_formal_task()
                except Exception as archive_error:  # noqa: BLE001
                    sys.stderr.write("!! formal checkpoint: %s\n" % archive_error)
            return result
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("!! %s: %s\n" % (type(e).__name__, e))
            try:
                return self._json({"error": "%s: %s" % (type(e).__name__, str(e)[:200])}, 500)
            except Exception:  # noqa: BLE001
                return

    # -------- endpoints

    def h_api_event(self):
        """记录产品交互；不调用模型，也不阻断用户操作。"""
        b = self._body()
        name = (b.get("name") or "").strip()[:80]
        if not name:
            return self._json({"error": "事件名为空"}, 400)
        if name in SEMANTIC_EVENT_NAMES:
            rec = record_semantic_event(
                name, cur(), b.get("data") if isinstance(b.get("data"), dict) else {},
                (b.get("session") or "")[:80], "client")
            return self._json({"ok": True, "event": (rec or {}).get("id")})
        rec = {"id": nid("e"), "skill": STATE["active"],
               "session": (b.get("session") or "")[:80], "name": name,
               "data": b.get("data") if isinstance(b.get("data"), dict) else {},
               "client_at": b.get("at"), "at": time.time()}
        with LOCK:
            STATE.setdefault("events", []).append(rec)
        return self._json({"ok": True})

    def h_api_study_questionnaire_start(self):
        """Present a post-decision prediction probe without revealing its execution."""
        b = self._body()
        sk = cur()
        if not sk or not sk.get("scenario_id"):
            return self._json({"error": "当前任务没有研究问卷"}, 400)
        review = latest_task_end_review(sk["id"])
        if not review:
            return self._json({"error": "请先完成发布或暂缓决定"}, 409)
        pack = scenario_rt.get_pack(sk["scenario_id"])
        cases = prediction_holdouts(pack)
        if not cases:
            return self._json({"error": "当前场景没有配置行为预测题"}, 404)
        session = (b.get("session") or "")[:80]
        formal = bool((sk.get("study_context") or {}).get("formal"))
        existing = next((row for row in reversed(STATE.get("questionnaires", []))
                         if row.get("skill") == sk["id"] and
                         row.get("review_id") == review.get("id") and
                         (formal or row.get("session") == session)), None)
        if existing:
            existing_ids = [row.get("case_id") for row in
                            existing.get("prediction_cases") or [] if row.get("case_id")]
            if not existing_ids and existing.get("case_id"):
                existing_ids = [existing["case_id"]]
            existing_cases = [scenario_rt.get_case(pack, case_id)
                              for case_id in existing_ids] or cases
            return self._json({"questionnaire": public_questionnaire(
                existing, existing_cases)})

        candidate = sk.get("candidate")
        if review.get("action") == "publish":
            instructions = copy.deepcopy(sk.get("instructions") or [])
            artifact_hash = sk.get("content_hash") or full_hash(instructions)
            artifact_version = sk.get("version")
        elif candidate:
            instructions = copy.deepcopy(candidate.get("instructions") or [])
            artifact_hash = candidate.get("content_hash") or full_hash(instructions)
            artifact_version = candidate.get("version")
        else:
            instructions = copy.deepcopy(sk.get("instructions") or [])
            artifact_hash = sk.get("content_hash") or full_hash(instructions)
            artifact_version = sk.get("version")
        prediction_cases = []
        question_schema = []
        for case in cases:
            snapshot = scenario_rt.case_snapshot(pack, case["id"])
            case_questions = copy.deepcopy(
                (case.get("study_measure") or {}).get("prediction_questions") or [])
            for question in case_questions:
                question["case_id"] = case["id"]
            prediction_cases.append({
                "case_id": case["id"], "case_hash": snapshot["case_hash"],
                "question_ids": [row["id"] for row in case_questions],
            })
            question_schema.extend(case_questions)
        if len({row.get("id") for row in question_schema}) != len(question_schema):
            return self._json({"error": "行为预测题 id 在多个留出情况中重复"}, 500)
        record = {
            "id": nid("q"), "skill": sk["id"], "session": session,
            "review_id": review.get("id"), "review_action": review.get("action"),
            "condition": review.get("condition") or "workspace",
            "study_context": copy.deepcopy(sk.get("study_context") or {}),
            "scenario_id": sk["scenario_id"], "case_id": cases[0]["id"],
            "case_hash": prediction_cases[0]["case_hash"],
            "prediction_cases": prediction_cases,
            "artifact": {"hash": artifact_hash, "version": artifact_version,
                         "instructions": instructions},
            "question_schema": question_schema,
            "status": "presented", "presented_at": time.time(),
        }
        with LOCK:
            STATE.setdefault("questionnaires", []).append(record)
        return self._json({"questionnaire": public_questionnaire(record, cases)})

    def h_api_study_questionnaire_submit(self):
        """Store post-task ratings, then execute the prediction holdout out of view."""
        b = self._body()
        record = next((row for row in STATE.get("questionnaires", [])
                       if row.get("id") == b.get("id") and
                       row.get("skill") == STATE["active"]), None)
        if not record:
            return self._json({"error": "问卷不存在或不属于当前任务"}, 404)
        if record.get("status") == "completed":
            try:
                archive = archive_formal_task(record.get("skill"), "completed")
            except Exception as archive_error:  # noqa: BLE001
                return self._json({
                    "error": "问卷已记录，但研究数据归档失败：%s" % archive_error
                }, 500)
            return self._json({"ok": True, "completed": True,
                               "archive_saved": bool(archive)})

        schema = {row["id"]: row for row in record.get("question_schema") or []}
        answers = b.get("predictions") if isinstance(b.get("predictions"), list) else []
        by_id = {row.get("question_id"): row for row in answers
                 if isinstance(row, dict) and row.get("question_id") in schema}
        if set(by_id) != set(schema):
            return self._json({"error": "请完成全部行为预测题"}, 400)
        normalized_answers = []
        for question_id, question in schema.items():
            row = by_id[question_id]
            allowed = [option.get("value") for option in question.get("options") or []]
            if row.get("value") not in allowed:
                return self._json({"error": "行为预测选项无效"}, 400)
            try:
                confidence = int(row.get("confidence"))
            except (TypeError, ValueError):
                return self._json({"error": "请填写每项预测的信心"}, 400)
            if confidence < 0 or confidence > 100:
                return self._json({"error": "预测信心需在 0 到 100 之间"}, 400)
            normalized_answers.append({"question_id": question_id,
                                       "case_id": question.get("case_id") or
                                                  record.get("case_id"),
                                       "value": copy.deepcopy(row.get("value")),
                                       "confidence": confidence})

        ratings = b.get("ratings") if isinstance(b.get("ratings"), dict) else {}
        workload = b.get("workload") if isinstance(b.get("workload"), dict) else {}
        rating_ids = {row["id"] for row in POST_TASK_ITEMS}
        workload_ids = {row["id"] for row in RAW_TLX_ITEMS}
        try:
            normalized_ratings = {key: int(ratings[key]) for key in rating_ids}
            normalized_workload = {key: int(workload[key]) for key in workload_ids}
        except (KeyError, TypeError, ValueError):
            return self._json({"error": "请完成全部体验和任务负荷题"}, 400)
        if any(value < 1 or value > 7 for value in normalized_ratings.values()):
            return self._json({"error": "体验评分需在 1 到 7 之间"}, 400)
        if any(value < 0 or value > 100 for value in normalized_workload.values()):
            return self._json({"error": "任务负荷评分需在 0 到 100 之间"}, 400)

        pack = scenario_rt.get_pack(record["scenario_id"])
        case_ids = [row.get("case_id") for row in record.get("prediction_cases") or []
                    if row.get("case_id")]
        if not case_ids:
            case_ids = [record["case_id"]]
        cases = [scenario_rt.get_case(pack, case_id) for case_id in case_ids]
        blind_case = research_holdout(pack)
        instructions = copy.deepcopy(record["artifact"]["instructions"])
        work = [("prediction", case["id"], scenario_rt.case_snapshot(pack, case["id"]))
                for case in cases for _ in range(3)]
        if blind_case:
            blind_snapshot = scenario_rt.case_snapshot(pack, blind_case["id"])
            work.extend(("research", blind_case["id"], blind_snapshot) for _ in range(3))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(work)) as executor:
            completed = list(executor.map(
                lambda item: (item[0], item[1], _scenario_exec_once(
                    instructions, copy.deepcopy(item[2]))), work))
        runs = [row for kind, _, row in completed if kind == "prediction"]
        research_runs = [row for kind, _, row in completed if kind == "research"]
        valid = [row for row in runs if not row.get("error")]
        result = {
            "run_count": len(runs), "valid_runs": len(valid),
            "technical_errors": [row.get("error") for row in runs if row.get("error")],
            "case_results": [],
        }
        scored = []
        modal_shares = []
        oracle_values = []
        for case in cases:
            case_runs = [row for kind, case_id, row in completed
                         if kind == "prediction" and case_id == case["id"]]
            case_valid = [row for row in case_runs if not row.get("error")]
            case_result = {
                "case_id": case["id"], "run_count": len(case_runs),
                "valid_runs": len(case_valid),
                "technical_errors": [row.get("error") for row in case_runs
                                     if row.get("error")],
            }
            if case_valid:
                signatures = {}
                for row in case_valid:
                    signature = json.dumps(row.get("facts") or {}, ensure_ascii=False,
                                           sort_keys=True, separators=(",", ":"))
                    signatures.setdefault(signature, []).append(row)
                modal_rows = max(signatures.values(), key=lambda rows: len(rows))
                facts = (modal_rows[-1].get("facts") or {})
                modal_share = len(modal_rows) / float(len(case_valid))
                modal_shares.append(modal_share)
                for answer in normalized_answers:
                    if answer.get("case_id") != case["id"]:
                        continue
                    question = schema[answer["question_id"]]
                    expected = facts.get(question.get("fact"))
                    scored.append({
                        "case_id": case["id"], "question_id": answer["question_id"],
                        "answer": copy.deepcopy(answer["value"]),
                        "expected": copy.deepcopy(expected),
                        "correct": answer["value"] == expected,
                        "confidence": answer["confidence"],
                    })
                case_oracle = sum(1 for row in case_valid
                                  if (row.get("_oracle") or {}).get(
                                      "all_required_passed")) / float(len(case_valid))
                oracle_values.extend(1 if (row.get("_oracle") or {}).get(
                    "all_required_passed") else 0 for row in case_valid)
                case_result.update({
                    "modal_share": modal_share,
                    "modal_facts": copy.deepcopy(facts),
                    "oracle_pass_rate": case_oracle,
                })
            result["case_results"].append(case_result)
        if scored:
            correct_confidence = [row["confidence"] for row in scored if row["correct"]]
            incorrect_confidence = [row["confidence"] for row in scored if not row["correct"]]
            result.update({
                "prediction_items": scored,
                "prediction_accuracy": sum(1 for row in scored if row["correct"]) /
                                       float(len(scored)) if scored else None,
                "mean_confidence": sum(row["confidence"] for row in scored) /
                                   float(len(scored)),
                "mean_confidence_correct": (sum(correct_confidence) /
                                            float(len(correct_confidence))
                                            if correct_confidence else None),
                "mean_confidence_incorrect": (sum(incorrect_confidence) /
                                              float(len(incorrect_confidence))
                                              if incorrect_confidence else None),
                "high_confidence_error_rate": (sum(1 for row in scored
                                                    if not row["correct"] and
                                                    row["confidence"] >= 80) /
                                               float(len(scored))),
                "modal_share": (sum(modal_shares) / float(len(modal_shares))
                                if modal_shares else None),
                "oracle_pass_rate": (sum(oracle_values) / float(len(oracle_values))
                                     if oracle_values else None),
            })
        research_valid = [row for row in research_runs if not row.get("error")]
        research_result = {
            "case_id": blind_case.get("id") if blind_case else None,
            "run_count": len(research_runs), "valid_runs": len(research_valid),
            "technical_errors": [row.get("error") for row in research_runs
                                 if row.get("error")],
            "oracle_pass_rate": (sum(1 for row in research_valid
                                     if (row.get("_oracle") or {}).get(
                                         "all_required_passed")) /
                                 float(len(research_valid))) if research_valid else None,
        }
        if research_valid:
            signatures = collections.Counter(
                json.dumps(row.get("facts") or {}, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) for row in research_valid)
            research_result["modal_share"] = max(signatures.values()) / float(len(research_valid))
        with LOCK:
            record["predictions"] = normalized_answers
            record["ratings"] = normalized_ratings
            record["workload"] = normalized_workload
            record["comment"] = (b.get("comment") or "").strip()[:2000]
            record["measurement"] = result
            record["holdout_runs"] = runs
            record["research_holdout"] = research_result
            record["research_holdout_runs"] = research_runs
            record["submitted_at"] = time.time()
            record["status"] = "completed"
        try:
            archive = archive_formal_task(record.get("skill"), "completed")
        except Exception as archive_error:  # noqa: BLE001
            return self._json({
                "error": "问卷已记录，但研究数据归档失败：%s" % archive_error
            }, 500)
        return self._json({"ok": True, "completed": True,
                           "measurement_status": "scored" if valid else "execution_failed",
                           "archive_saved": bool(archive)})

    def h_api_reset(self):
        """Clear the in-memory workspace; the client may then reload its current scenario pack."""
        with LOCK:
            STATE["skills"].clear()
            STATE["active"] = None
            STATE["snapshots"].clear()
            for key in ("runs", "situations", "probes", "reviews", "events", "chat",
                        "questionnaires"):
                STATE.setdefault(key, []).clear()
            STATE["seq"] = 0
        return self._json({"ok": True})

    def h_api_scenario_load(self):
        """Open a frozen historical incident without exposing reference answers."""
        b = self._body()
        try:
            pack = scenario_rt.get_pack(b.get("id"))
        except KeyError as exc:
            return self._json({"error": str(exc)}, 404)
        raw_study = b.get("study") if isinstance(b.get("study"), dict) else {}
        if raw_study.get("formal") is True:
            required = (raw_study.get("session"), raw_study.get("participant"),
                        raw_study.get("period"))
            if any(not str(value or "").strip() for value in required) or \
                    raw_study.get("condition") not in ("workspace", "chat"):
                return self._json({
                    "error": "正式任务缺少 session、participant、condition 或 period"
                }, 400)
            assignment_problem = configured_assignment_problem(raw_study, pack)
            if assignment_problem:
                return self._json({"error": assignment_problem}, 409)
            try:
                ensure_study_archive_ready()
            except Exception as archive_error:  # noqa: BLE001
                return self._json({"error": str(archive_error)}, 503)
            # A formal instance is one participant × condition × task period.
            # Never let a later load silently replace or mix that assignment.
            if cur():
                return self._json({
                    "error": "当前实例已有正式任务状态；请刷新原任务或使用新的隔离实例"
                }, 409)
        elif b.get("reuse") is True:
            existing = next((skill for skill in STATE["skills"].values()
                             if skill.get("scenario_id") == pack["id"]), None)
            if existing:
                with LOCK:
                    STATE["active"] = existing["id"]
                snapshots = [row for row in STATE["snapshots"].values()
                             if row.get("skill") == existing["id"]]
                snap = next((row for row in snapshots
                             if row.get("case_role") == "incident"),
                            snapshots[0] if snapshots else None)
                if snap:
                    record_semantic_event("task_started", existing, {
                        "task_hash": (existing.get("study_context") or {}).get("task_hash"),
                        "restored": True,
                    }, (existing.get("study_context") or {}).get("session", ""))
                    return self._json({
                        "skill": existing,
                        "snapshot": snap,
                        "work_order": copy.deepcopy(existing.get("work_order")),
                        "study_context": copy.deepcopy(
                            existing.get("study_context") or {}),
                        "scenario": next(row for row in scenario_rt.public_scenarios()
                                         if row["id"] == pack["id"]),
                        "restored": True,
                    })
        parsed = scenario_rt.skill_record(pack)
        order = scenario_rt.public_work_order(pack)
        assignment = study_context(b.get("study"), pack)
        skill_id = nid("k")
        sk = initialize_skill_record({
            "id": skill_id,
            "name": parsed["name"],
            "instructions": parsed["instructions"],
            "tools": parsed["tools"],
            "sources": parsed["sources"],
            "config": parsed["config"],
            "version": 1,
            "scenario_id": parsed["scenario_id"],
            "scenario_pack_hash": parsed["scenario_pack_hash"],
            "work_order": order,
            "study_context": assignment,
            "versions": [],
            "candidate": None,
            "scope_version": 1,
        })
        snap = scenario_rt.case_snapshot(pack, pack["entry_case"])
        snap.update({"id": nid("s"), "skill": skill_id, "recorded": time.time()})
        with LOCK:
            STATE["skills"][skill_id] = sk
            STATE["active"] = skill_id
            STATE["snapshots"][snap["id"]] = snap
        record_semantic_event("task_started", sk, {
            "task_hash": assignment.get("task_hash"), "scenario_id": pack["id"],
        }, assignment.get("session", ""))
        return self._json({
            "skill": sk,
            "snapshot": snap,
            "work_order": order,
            "study_context": assignment,
            "scenario": next(row for row in scenario_rt.public_scenarios()
                             if row["id"] == pack["id"]),
        })

    def h_api_skill(self):
        b = self._body()
        out = ask(P_PARSE, b.get("text", "")[:12000], 4000)
        if not out or "instructions" not in out:
            return self._json(out or {"error": "解析失败"}, 500)
        sid = nid("k")
        sk = initialize_skill_record({
            "id": sid, "name": out.get("name") or "未命名",
            "instructions": out["instructions"], "tools": out.get("tools") or [],
            "config": out.get("config") or {}, "version": 1,
            "versions": [], "candidate": None, "scope_version": 1,
            "sources": {t["name"]: {"label": t.get("label") or t["name"],
                                      "kind": t.get("kind") or
                                      ("write" if t.get("side_effecting") else "query"),
                                      "signature": t.get("signature") or "",
                                      "returns": t.get("returns") or "", "rows": []}
                          for t in (out.get("tools") or [])},
        })
        with LOCK:
            STATE["skills"][sid] = sk
            STATE["active"] = sid
        return self._json({"skill": sk})

    def h_api_skill_select(self):
        i = self._body().get("id")
        if i not in STATE["skills"]:
            return self._json({"error": "skill 不存在"}, 400)
        with LOCK:
            STATE["active"] = i
        return self._json({"skill": cur()})

    def h_api_skill_rename(self):
        b = self._body()
        sk = STATE["skills"].get(b.get("id") or STATE["active"])
        if not sk:
            return self._json({"error": "skill 不存在"}, 400)
        name = (b.get("name") or "").strip()
        if not name:
            return self._json({"error": "名称为空"}, 400)
        with LOCK:
            sk["name"] = name[:60]
            if sk.get("candidate"):
                sk["candidate"]["name"] = sk["name"]
        return self._json({"skill": sk})

    def h_api_skill_delete(self):
        i = self._body().get("id")
        if i not in STATE["skills"]:
            return self._json({"error": "skill 不存在"}, 400)
        with LOCK:
            STATE["skills"].pop(i)
            for k in [k for k, v in STATE["snapshots"].items() if v.get("skill") == i]:
                STATE["snapshots"].pop(k)
            STATE["runs"][:] = [x for x in STATE["runs"] if x.get("skill") != i]
            STATE["situations"][:] = [x for x in STATE["situations"] if x.get("skill") != i]
            STATE["probes"][:] = [x for x in STATE["probes"] if x.get("skill") != i]
            STATE["reviews"][:] = [x for x in STATE.get("reviews", []) if x.get("skill") != i]
            STATE["events"][:] = [x for x in STATE.get("events", []) if x.get("skill") != i]
            STATE["chat"][:] = [x for x in STATE.get("chat", []) if x.get("skill") != i]
            STATE["questionnaires"][:] = [x for x in STATE.get("questionnaires", [])
                                           if x.get("skill") != i]
            if STATE["active"] == i:
                STATE["active"] = next(iter(STATE["skills"]), None)
        return self._json({"ok": True, "active": STATE["active"]})

    def h_api_export(self):
        """评审记录：skill 版本、快照、执行、情况与判据、探测与结论。"""
        document = export_document()
        if not document:
            return self._json({"error": "无 skill"}, 400)
        return self._json(document)

    def h_api_import(self):
        b = self._body()
        sk = b.get("skill")
        if not sk or "instructions" not in sk:
            return self._json({"error": "包格式不正确"}, 400)
        sid = nid("k")
        sk = dict(sk)
        sk["id"] = sid
        initialize_skill_record(sk)
        with LOCK:
            STATE["skills"][sid] = sk
            STATE["active"] = sid
            for snap in b.get("snapshots") or []:
                snap = dict(snap); snap["skill"] = sid
                # Older exports may contain a pre-authored incident card. It
                # must not reappear before a fresh execution-derived analysis.
                if snap.get("case_role") == "incident":
                    snap.pop("review_prompt", None)
                STATE["snapshots"][snap["id"]] = snap
            STATE.setdefault("reviews", [])
            STATE.setdefault("events", [])
            STATE.setdefault("chat", [])
            STATE.setdefault("questionnaires", [])
            for key, dst in (("runs", STATE["runs"]), ("situations", STATE["situations"]),
                             ("probes", STATE["probes"]), ("reviews", STATE["reviews"]),
                             ("events", STATE["events"]), ("chat", STATE["chat"]),
                             ("questionnaires", STATE["questionnaires"])):
                for x in b.get(key) or []:
                    x = dict(x); x["skill"] = sid; dst.append(x)
            for snap in STATE["snapshots"].values():
                if snap.get("skill") != sid or snap.get("baseline_completed_at"):
                    continue
                base_runs = [row for row in STATE["runs"]
                             if row.get("skill") == sid and row.get("sid") == snap.get("id")
                             and not row.get("variant") and not row.get("error")]
                if base_runs:
                    snap["baseline_completed_at"] = time.time()
                    snap["baseline_run_ids"] = [row["id"] for row in base_runs]
            STATE["seq"] = max(STATE["seq"], max_imported_sequence(b))
        return self._json({"skill": sk})

    def h_api_intent(self):
        return self._json(ask(P_INTENT, self._body().get("text", "")[:2000], 900)
                          or {"intent": "other"})

    def h_api_snapshot(self):
        """建立一次任务的输入记录：读用户数据源，向外部服务发起查询并记录其返回。"""
        b = self._body()
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        task = b.get("task", "")
        if sk.get("scenario_id"):
            pack = scenario_rt.get_pack(sk["scenario_id"])
            case_id = b.get("case_id")
            case = None
            if case_id:
                try:
                    case = scenario_rt.get_case(pack, case_id)
                except KeyError:
                    return self._json({"error": "情况不在当前冻结场景中"}, 404)
            else:
                case = next((row for row in pack.get("cases", [])
                             if row.get("task") == task
                             and not scenario_rt.participant_hidden(row)), None)
            if not case or scenario_rt.participant_hidden(case):
                return self._json({
                    "error": "当前研究工作区只执行已经冻结的历史情况和发布前检查。",
                    "scenario": sk["scenario_id"],
                }, 400)
            snap = scenario_rt.case_snapshot(pack, case["id"])
            snap.update({"id": nid("s"), "skill": STATE["active"], "recorded": time.time()})
            with LOCK:
                STATE["snapshots"][snap["id"]] = snap
            return self._json({"snapshot": snap})
        srcs = sk.get("sources") or {}
        tools, args, missing = {}, {}, []

        ordered = sorted(srcs.items(),
                         key=lambda kv: {"data": 0, "query": 1, "write": 2}.get(kv[1].get("kind"), 1))
        for name, src in ordered:
            kind = src.get("kind")
            if kind == "write":
                continue                       # 状态变更工具不预取，只在隔离执行环境中调用
            if kind == "data":
                if src.get("rows"):
                    tools[name] = src["rows"]
                else:
                    missing.append(name)
                continue
            # query：先只从任务里取出调用参数，再把参数交给连接器
            owned = {k: v for k, v in tools.items()
                     if (srcs.get(k) or {}).get("kind") == "data"}
            fxs = src.get("fixture") or []
            sample = ("\n\nSAMPLE RECORDS FROM THIS SERVICE:\n"
                      + json.dumps(fxs[:3], ensure_ascii=False)) if fxs else ""
            qa = ask(P_ARGS, "TOOL: %s%s -> %s%s\n\nUSER DATA:\n%s\n\nTASK: %s" % (
                name, src.get("signature") or "", src.get("returns") or "", sample,
                json.dumps(owned, ensure_ascii=False)[:2500], task), 900)
            a_ = (qa or {}).get("args") or {}
            args[name] = a_
            fx = src.get("fixture")
            if fx:
                got = query_fixture(fx, a_)
            else:
                res = ask(P_CONNECT, "SERVICE: %s%s -> %s\n\nPARAMETERS: %s" % (
                    name, src.get("signature") or "", src.get("returns") or "",
                    json.dumps(a_, ensure_ascii=False)), 3000)
                got = res.get("result") if res else None
            if got is None or (isinstance(got, (list, dict)) and len(got) == 0):
                # 外部服务没有返回任何结果：如实报缺，不让执行在空集合上继续
                missing.append(name)
            else:
                tools[name] = got

        if not tools:
            return self._json({"error": "没有可用的数据源", "missing": missing}, 400)

        snap = {"id": nid("s"), "task": task, "tools": tools, "args": args,
                "missing": missing, "summary": "", "fact_schema": None,
                "skill": STATE["active"], "recorded": time.time()}
        with LOCK:
            STATE["snapshots"][snap["id"]] = snap
        return self._json({"snapshot": snap})

    def h_api_case(self):
        """从现有记录派生一个相邻情况，但为它生成并冻结一套新的完整输入。"""
        b = self._body()
        base = STATE["snapshots"].get(b.get("snapshot"))
        if not base or base.get("skill") != STATE["active"]:
            return self._json({"error": "基础情况不存在"}, 400)
        description = (b.get("description") or "").strip()
        if not description:
            return self._json({"error": "缺少情况描述"}, 400)
        if base.get("runtime") == scenario_rt.RUNTIME_SCHEMA:
            pack = scenario_rt.get_pack(base["scenario_id"])
            allowed = {row["case_id"] for row in
                       scenario_rt.get_case(pack, base["case_id"]).get("neighbours", [])}
            case_id = b.get("case_id")
            if not case_id:
                case_id = next((row["case_id"] for row in
                                scenario_rt.get_case(pack, base["case_id"]).get("neighbours", [])
                                if scenario_rt.get_case(pack, row["case_id"])["summary"] == description),
                               None)
            if case_id not in allowed:
                return self._json({"error": "该情况不在这次发布前检查集合中"}, 400)
            snap = scenario_rt.case_snapshot(pack, case_id)
            snap.update({
                "id": nid("s"), "skill": STATE["active"], "recorded": time.time(),
                "parent_snapshot": base["id"], "requested_situation": description,
            })
            with LOCK:
                STATE["snapshots"][snap["id"]] = snap
            return self._json({"snapshot": snap,
                               "changed_factors": snap.get("changed_factors", [])})
        prompt = ("REQUESTED NEIGHBOURING SITUATION: %s\nRELATION TO BASE: %s\n\n"
                  "BASE TASK: %s\n\nBASE FROZEN TOOL RESULTS:\n%s" % (
                      description, b.get("relation") or "", base.get("task") or "",
                      json.dumps(base.get("tools") or {}, ensure_ascii=False, indent=1)[:10000]))
        out = ask(P_CASE, prompt, 5000)
        if not out or "error" in out or not isinstance(out.get("tools"), dict):
            return self._json(out or {"error": "相关情况生成失败"}, 500)

        # 模型只能替换已有工具的返回值，不能偷偷增删工具或复用可变引用。
        tools = copy.deepcopy(base.get("tools") or {})
        for name in list(tools):
            if name in out["tools"]:
                tools[name] = out["tools"][name]
        changed = [str(x)[:120] for x in (out.get("changed_factors") or [])[:4]]
        snap = {"id": nid("s"), "task": (out.get("task") or description).strip(),
                "tools": tools, "args": copy.deepcopy(base.get("args") or {}),
                "missing": [], "summary": out.get("summary") or description,
                "fact_schema": copy.deepcopy(base.get("fact_schema")),
                "skill": STATE["active"], "recorded": time.time(),
                "source": "generated", "parent_snapshot": base["id"],
                "changed_factors": changed, "requested_situation": description}
        with LOCK:
            STATE["snapshots"][snap["id"]] = snap
        return self._json({"snapshot": snap, "changed_factors": changed})

    def h_api_source(self):
        """写入用户自己的数据。rows 为该数据源的全部内容。"""
        b = self._body()
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        name = b.get("tool")
        src = (sk.get("sources") or {}).get(name)
        if not src:
            return self._json({"error": "数据源不存在"}, 400)
        with LOCK:
            src["rows"] = b.get("rows") or []
        return self._json({"source": src})

    def h_api_fixture(self):
        """为外部服务载入后台固定表。仅供实验准备，不在界面暴露。"""
        b = self._body()
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        n = 0
        with LOCK:
            for name, rows in (b.get("fixtures") or {}).items():
                src = (sk.get("sources") or {}).get(name)
                if src is not None and src.get("kind") == "query":
                    src["fixture"] = rows
                    n += 1
        return self._json({"loaded": n})

    def h_api_seed(self):
        """批量灌入数据源内容（实验用的预制数据走同一条路径）。"""
        b = self._body()
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        n = 0
        with LOCK:
            for name, rows in (b.get("sources") or {}).items():
                src = (sk.get("sources") or {}).get(name)
                if src is not None:
                    src["rows"] = rows
                    n += 1
        return self._json({"seeded": n})

    def _stream_runs(self, instructions, snap, variant, k, crit, on_run=None, quiet=False):
        acc = []
        artifact_hash = full_hash(instructions)
        with concurrent.futures.ThreadPoolExecutor(k) as ex:
            futs = [ex.submit(exec_once, instructions, snap, variant.get("perturb"))
                    for _ in range(k)]
            for fut in concurrent.futures.as_completed(futs):
                r = fut.result() or {"error": "空返回"}
                r["_pass"] = eval_criterion(crit, r) if crit else None
                r.update({"id": nid("r"), "sid": snap["id"], "variant": variant,
                          "skill": STATE["active"], "artifact_hash": artifact_hash,
                          "snapshot_hash": snap.get("world_hash") or full_hash({
                              "task": snap.get("task"), "tools": snap.get("tools")})})
                acc.append(r)
                with LOCK:
                    STATE["runs"].append(r)
                if not quiet:
                    public_run = {key: value for key, value in r.items() if key != "_oracle"}
                    self._chunk({"type": "run", "run": public_run})
                if on_run:
                    on_run(r)
        return acc

    def h_api_run(self):
        b = self._body()
        snap = STATE["snapshots"].get(b.get("snapshot"))
        if not snap or not cur():
            return self._json({"error": "快照不存在"}, 400)
        variant = b.get("variant") or {}
        raw_k = b.get("k", BASELINE_RUNS)
        if isinstance(raw_k, (bool, dict, list)):
            return self._json({"error": "运行次数必须是 1 到 8 的整数"}, 400)
        try:
            k = int(raw_k)
        except (TypeError, ValueError):
            return self._json({"error": "运行次数必须是 1 到 8 的整数"}, 400)
        if k < 1 or k > 8:
            return self._json({"error": "运行次数必须是 1 到 8 的整数"}, 400)
        crit = b.get("criterion")
        if not snap.get("fact_schema"):
            fs = ask(P_FACTS, "TASK: %s\nTOOLS: %s" % (snap["task"], ", ".join(snap["tools"])), 900)
            if fs and "keys" in fs:
                snap["fact_schema"] = fs["keys"]
        instructions, base = instructions_for(variant)
        self._open()
        acc = self._stream_runs(instructions, snap, variant, k, crit)
        if not variant:
            with LOCK:
                snap["baseline_completed_at"] = time.time()
                snap["baseline_run_ids"] = [row["id"] for row in acc]
        if variant.get("draft"):
            with LOCK:
                if cur() and not cur().get("first_candidate_revealed_at"):
                    cur()["first_candidate_revealed_at"] = time.time()
                if cur():
                    cur()["last_compare_budget"] = k
                active_round = (cur() or {}).get("review_round") or 1
                for situation in STATE["situations"]:
                    if situation.get("skill") == STATE["active"] and \
                            situation.get("sid") == snap["id"] and \
                            not situation.get("superseded_at") and \
                            (situation.get("review_round") or 1) == active_round:
                        if not situation.get("candidate_outcome_revealed_at"):
                            situation["candidate_outcome_revealed_at"] = time.time()
        self._chunk({"type": "done", "summary": summarize(acc, crit,
                                                          (snap.get("fact_schema") or [])[:1]),
                     "skill_hash": base["hash"], "snapshot": snap["id"]})
        self._close()

    def h_api_issue(self):
        """Derive a product-facing conflict from a completed baseline tool run."""
        b = self._body()
        snap = STATE["snapshots"].get(b.get("snapshot"))
        sk = cur()
        if not snap or not sk or snap.get("skill") != STATE["active"]:
            return self._json({"error": "快照不存在"}, 400)
        if not snap.get("scenario_id") or snap.get("case_role") != "incident":
            return self._json({"issue": None, "source": "not-applicable"})
        runs = [row for row in mine(STATE["runs"])
                if row.get("sid") == snap["id"] and not row.get("variant")
                and not row.get("error")]
        if not snap.get("baseline_completed_at"):
            return self._json({"error": "初始执行尚未完成"}, 409)
        if not runs:
            return self._json({"error": "没有可分析的执行结果"}, 409)

        groups = {}
        for row in runs:
            signature = json.dumps(row.get("facts") or {}, ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":"))
            groups.setdefault(signature, []).append(row)
        representative = max(groups.values(), key=lambda rows: (len(rows), rows[-1]["id"]))[-1]
        pack = scenario_rt.get_pack(snap["scenario_id"])
        issue = scenario_rt.analyze_issue(pack, snap, representative)
        analysis = {
            "source": "tool-trace-and-world-state",
            "run_id": representative["id"],
            "facts_hash": full_hash(representative.get("facts") or {}),
            "issue_found": bool(issue),
            "completed_at": time.time(),
        }
        with LOCK:
            snap["issue_analysis"] = analysis
        return self._json({"issue": issue, "analysis": analysis})

    def h_api_criterion(self):
        b = self._body()
        sid = b.get("snapshot")
        runs = [r for r in mine(STATE["runs"]) if r.get("sid") == sid and "error" not in r
                and not r.get("variant")]
        keys = sorted({k for r in runs for k in (r.get("facts") or {})})
        kinds = sorted({s.get("kind") for r in runs for s in r.get("steps", []) if s.get("kind")})
        sample = json.dumps(runs[-1], ensure_ascii=False)[:1500] if runs else "{}"
        out = ask(P_CRIT, "EXPECTATION: %s\n\nFACT KEYS: %s\n\nSTEP KINDS: %s\n\nSAMPLE:\n%s"
                  % (b.get("commitment", ""), keys, kinds, sample), 2500)
        if not out or "candidates" not in out:
            return self._json(out or {"error": "判据生成失败"}, 500)
        out["candidates"] = [row for row in out.get("candidates") or []
                             if row.get("form") in ("trace", "fact")]
        if not out["candidates"]:
            return self._json({"error": "没有生成可由工具轨迹或事实重建的验收条件"}, 500)
        trial = runs[-4:]
        for c in out["candidates"]:
            c["trial"] = [eval_criterion(c, r) for r in trial]
        return self._json({"candidates": out["candidates"], "tested_on": len(trial)})

    def h_api_contrast(self):
        """针对刚说出的改动，提出可能被牵连的相邻情况，交给用户表态。"""
        b = self._body()
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        snap = STATE["snapshots"].get(b.get("snapshot")) or _last_snap()
        if sk.get("scenario_id") and snap and snap.get("scenario_id") == sk["scenario_id"]:
            plan = ensure_scope_plan(sk, b.get("commitment"))
            if not plan:
                return self._json({"error": "请先写明本次修复目标"}, 400)
            return self._json({"situations": copy.deepcopy(plan.get("cases") or []),
                               "intent": copy.deepcopy(plan.get("intent") or {}),
                               "plan_id": plan.get("id"), "plan_hash": plan.get("hash"),
                               "source": plan.get("source")})
        lines = "\n".join("%d. %s" % (i["n"], i["text"]) for i in sk["instructions"])
        have = [x["commitment"] for x in mine(STATE["situations"])]
        out = ask(P_CONTRAST, "SKILL:\n%s\n\nWHAT THEY WANT CHANGED: %s\n\nALREADY RECORDED: %s"
                  % (lines, b.get("commitment", ""), json.dumps(have, ensure_ascii=False)), 1500)
        return self._json(out or {"situations": []})

    def h_api_prepare_cases(self):
        """Run the same intent-conditioned related cases used by chat mode."""
        b = self._body()
        sk = cur()
        if not sk or not sk.get("scenario_id"):
            return self._json({"error": "当前 Skill 没有冻结的相关情况"}, 400)
        prepared = op_prepare_related_cases(
            k=BASELINE_RUNS, commitment=(b.get("commitment") or "").strip() or None)
        if prepared.get("error"):
            return self._json(prepared, 409)
        return self._json(prepared)

    def h_api_situation(self):
        b = self._body()
        sk0 = cur()
        if sk0 and sk0.get("candidate"):
            return self._json({"error": "已存在草稿，判定标准在本轮评审中锁定"}, 409)
        now = time.time()
        snap = STATE["snapshots"].get(b.get("snapshot")) or {}
        disposition = (b.get("disposition") or "").strip().lower()
        if disposition not in ("change", "preserve", "unresolved", "excluded"):
            return self._json({"error": "判断类型无效"}, 400)
        if disposition == "excluded" and snap.get("case_role") == "incident":
            return self._json({"error": "问题场景必须保留一项明确的修复目标"}, 400)
        commitment = (b.get("commitment") or "").strip()
        if not commitment:
            return self._json({"error": "判断内容为空"}, 400)
        sealed = bool(b.get("sealed"))
        st = {"id": nid("t"), "skill": STATE["active"], "sid": b.get("snapshot"),
              "commitment": commitment,
              "criterion": b.get("criterion"), "disposition": disposition,
              "label": b.get("label", ""), "sealed": sealed,
              "case_source": b.get("case_source") or "user",
              "case_context": b.get("case_context") or "",
              "parent_snapshot": b.get("parent_snapshot"),
              "changed_factors": b.get("changed_factors") or [],
              "case_id": snap.get("case_id"), "case_hash": snap.get("case_hash"),
              "pre_reveal": not bool((sk0 or {}).get("first_candidate_revealed_at")),
              "review_round": (sk0 or {}).get("review_round") or 1,
              "judged_before_candidate_in_round": True,
              "post_reveal": bool((sk0 or {}).get("first_candidate_revealed_at")),
              "judged_at": now, "owner_revealed_at": now,
              "generator_exposure": "withheld" if sealed else "visible",
              "candidate_outcome_revealed_at": None,
              "created": now}
        with LOCK:
            sk = cur()
            if sk:
                sk["scope_version"] = (sk.get("scope_version") or 1) + 1
                st["scope_version"] = sk["scope_version"]
                sk["scope_revision_required"] = False
                sk["active_repair_preview_id"] = None
            target_case_id = snap.get("case_id")
            active_round = (sk or {}).get("review_round") or 1
            matches = [row for row in STATE["situations"]
                       if row.get("skill") == STATE["active"] and not row.get("superseded_at") and
                       (row.get("review_round") or 1) == active_round and
                       (row.get("case_id") or
                        (STATE["snapshots"].get(row.get("sid")) or {}).get("case_id")) ==
                       target_case_id]
            if matches:
                keep = matches[-1]
                st["id"] = keep.get("id") or st["id"]
                st["created"] = keep.get("created") or st["created"]
                STATE["situations"] = [row for row in STATE["situations"]
                                       if row not in matches]
            STATE["situations"].append(st)
            scope_record = freeze_scope_version(sk) if sk else None
        if sk and disposition == "change" and snap.get("case_role") == "incident":
            record_semantic_event("intent_committed", sk, {
                "case_id": target_case_id, "scope_version": sk.get("scope_version") or 1,
                "scope_hash": (scope_record or {}).get("hash"),
            })
        if sk and scope_readiness(sk).get("ready"):
            record_semantic_event("scope_committed", sk, {
                "scope_version": sk.get("scope_version") or 1,
                "scope_hash": (scope_record or {}).get("hash"),
                "item_count": len(scope_items(sk["id"])),
            })
        return self._json({"situation": st, "scope_version": (sk or {}).get("scope_version"),
                           "scope_hash": (scope_record or {}).get("hash")})

    def h_api_probe(self):
        b = self._body()
        snap = STATE["snapshots"].get(b.get("snapshot"))
        if not snap:
            return self._json({"error": "快照不存在"}, 400)
        kind, n, sk = b.get("kind"), b.get("n"), cur()
        if kind == "delete":
            variant, note = {"mask": [n]}, "移除指令 %d" % n
        elif kind == "invert":
            src = [i for i in sk["instructions"] if i["n"] == n]
            if not src:
                return self._json({"error": "指令不存在"}, 400)
            inv = ask(P_INVERT, src[0]["text"], 900)
            if not inv or "text" not in inv:
                return self._json({"error": "反转生成失败"}, 500)
            variant, note = {"rewrite": {"n": n, "text": inv["text"]}}, inv["text"]
        elif kind == "perturb":
            p = ask(P_PERTURB, "TASK: %s\n\nTOOL RESULTS:\n%s" % (
                snap["task"], json.dumps(snap["tools"], ensure_ascii=False)[:4000]), 1500)
            if not p or "tool" not in p:
                return self._json({"error": "扰动生成失败"}, 500)
            variant = {"perturb": p}
            note = "%s%s：%s → %s" % (p.get("tool"), p.get("path"), p.get("from"), p.get("to"))
        else:
            return self._json({"error": "未知探测类型"}, 400)

        base_runs = [r for r in mine(STATE["runs"]) if r.get("sid") == snap["id"]
                     and not r.get("variant") and "error" not in r
                     and r.get("artifact_hash") == sk.get("content_hash")]
        base_mode = None

        k = max(1, min(6, int(b.get("k", 4))))
        instructions, _ = instructions_for(variant)
        self._open()
        self._chunk({"type": "probe", "kind": kind, "n": n, "note": note, "variant": variant,
                     "baseline": {"n": len(base_runs)}})
        acc = self._stream_runs(instructions, snap, variant, k, None)
        dk = discriminating(base_runs, acc)
        base_mode, base_share = modal(sig_by(r, dk) for r in base_runs)
        probe_mode, probe_share = modal(sig_by(r, dk) for r in acc)

        # 众数比较：单次差异不算变化，主导行为改变才算
        if base_mode is None or probe_mode is None:
            changed, conf = None, "no_baseline"
        elif base_share < 0.6 or probe_share < 0.6:
            changed, conf = (base_mode != probe_mode), "unstable"
        else:
            changed, conf = (base_mode != probe_mode), "ok"

        rec = {"id": nid("p"), "skill": STATE["active"], "kind": kind, "n": n,
               "note": note, "sid": snap["id"],
               "changed": changed, "confidence": conf, "k": len(acc),
               "base_share": round(base_share, 2) if base_share else None,
               "probe_share": round(probe_share, 2) if probe_share else None,
               "summary": summarize(acc)}
        rec.update(intervention_provenance(
            sk, snap, variant, instructions, base_runs, acc, "source_location"))
        with LOCK:
            STATE["probes"].append(rec)
        self._chunk({"type": "done", "probe": rec, "verdict": verdict_for(n)})
        self._close()

    def h_api_influence(self):
        """一个动作回答「这句话有用吗」：内部跑删除与反转，返回结论与它控制的步骤。"""
        b = self._body()
        snap = STATE["snapshots"].get(b.get("snapshot"))
        sk = cur()
        if not snap or not sk:
            return self._json({"error": "快照不存在"}, 400)
        n = b.get("n")
        src = [i for i in sk["instructions"] if i["n"] == n]
        if not src:
            return self._json({"error": "指令不存在"}, 400)

        base_runs = [r for r in mine(STATE["runs"]) if r.get("sid") == snap["id"]
                     and not r.get("variant") and "error" not in r
                     and r.get("artifact_hash") == sk.get("content_hash")]
        base_mode = None
        k = max(1, min(5, int(b.get("k", SOURCE_PROBE_RUNS))))

        self._open()
        self._chunk({"type": "start", "n": n})

        results = {}
        for kind in ("delete", "invert"):
            if kind == "delete":
                variant, note = {"mask": [n]}, None
            else:
                inv = ask(P_INVERT, src[0]["text"], 900)
                if not inv or "text" not in inv:
                    results[kind] = None
                    continue
                variant, note = {"rewrite": {"n": n, "text": inv["text"]}}, inv["text"]
            self._chunk({"type": "phase", "kind": kind})
            instructions, _ = instructions_for(variant)
            acc = self._stream_runs(instructions, snap, variant, k, None, quiet=True)
            dk = discriminating(base_runs, acc)
            chg, diff, weak = compare_fields(base_runs, acc, dk)
            rec = {"id": nid("p"), "skill": STATE["active"], "kind": kind, "n": n,
                   "note": note or ("移除指令 %d" % n), "sid": snap["id"],
                   "changed": chg, "changed_fields": diff,
                   "confidence": "unstable" if weak else "ok",
                   "base_share": None, "probe_share": None, "k": len(acc)}
            rec.update(intervention_provenance(
                sk, snap, variant, instructions, base_runs, acc, "source_location"))
            with LOCK:
                STATE["probes"].append(rec)
            results[kind] = rec

        d, i = results.get("delete"), results.get("invert")
        # 只有基线自身波动才让结论失效；探测结果本身的波动不影响「变没变」
        if not d or not i or d["changed"] is None or i["changed"] is None:
            code = "unsure"
        elif d["changed"] is False and i["changed"] is False:
            code = "dead"
        else:
            code = "controls"

        # 它控制哪些步骤：主导行为组里自述来自该指令的步骤
        steps = []
        if code == "controls" and base_runs:
            for r in reversed(base_runs):
                ix = [j for j, st in enumerate(r.get("steps") or []) if st.get("from") == n]
                if ix:
                    steps = ix
                    break
        self._chunk({"type": "done", "code": code, "steps": steps,
                     "detail": {"delete": d, "invert": i}})
        self._close()

    def h_api_chat(self):
        """条件 B：工具调用由系统主动编排，owner 只处理真实决策点。"""
        b = self._body()
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        msg = (b.get("message") or "").strip()
        hist = b.get("history") or []
        STATE.setdefault("chat", [])
        chat_record = {
            "id": nid("c"), "skill": STATE["active"],
            "session": (b.get("session") or "")[:80], "at": time.time(),
            "message": msg, "history_length": len(hist),
            "capability": None, "args": {}, "actions": [],
            "result": None, "reply": None,
        }
        with LOCK:
            STATE.setdefault("chat", []).append(chat_record)

        self._open()
        result = None
        reply = None
        questionnaire_path = None

        def emit(capability, detail=None):
            action = {"capability": capability, "detail": copy.deepcopy(detail or {})}
            chat_record["actions"].append(action)
            self._chunk({"type": "act", "capability": capability,
                         "label": (detail or {}).get("label")})

        def run_capability(capability, args):
            """Compatibility path for explicit side questions and release decisions."""
            if capability == "run_task":
                return op_run(args.get("task") or msg)
            if capability == "show_options":
                snap = _last_snap()
                if not snap:
                    return {"error": "还没有执行过任务"}
                srcs = sk.get("sources") or {}
                opts, chosen = None, None
                for tname, val in snap["tools"].items():
                    if (srcs.get(tname) or {}).get("kind") == "query" and \
                            isinstance(val, list) and val:
                        opts = val
                        break
                runs = [r for r in mine(STATE["runs"]) if r.get("sid") == snap["id"]
                        and not r.get("variant") and "error" not in r]
                if runs:
                    chosen = runs[-1].get("facts")
                return {"options": opts, "chosen_facts": chosen} if opts else \
                    {"error": "本次没有候选集合"}
            if capability == "check_instruction":
                return op_probe("instruction", int(args.get("n") or 0))
            if capability == "check_candidate_block":
                return op_block_probe(args.get("n"), args.get("case_id"), BLOCK_PROBE_RUNS)
            if capability == "check_data":
                return op_probe("data")
            if capability == "suggest_cases":
                return op_suggest_cases()
            if capability == "open_case":
                return op_open_case(args.get("case_id"))
            if capability == "record_expectation":
                return op_expect(args.get("text") or msg,
                                 args.get("disposition") or "change", args.get("case_id"))
            if capability == "draft":
                return self._draft_internal(bool(args.get("confirm")))
            if capability == "edit_instruction":
                return op_edit_instruction(args.get("n"), args.get("text"))
            if capability == "compare":
                return op_compare()
            if capability == "reopen_scope":
                return begin_scope_revision(sk, "chat-owner-requested-scope-revision")
            if capability == "decide":
                return self._decide_internal(args.get("action"), args.get("reason") or "")
            if capability == "list_instructions":
                return {"instructions": sk["instructions"]}
            return {"note": "未调用任何能力"}

        def route_explicit_request():
            incident_snap = _incident_snap()
            available = []
            if sk.get("scenario_id") and incident_snap:
                for row in related_case_contexts(sk):
                    available.append({"case_id": row["case_id"], "summary": row["summary"],
                                      "changed_factors": row["changed_factors"]})
            ctx = ("SKILL: %s\nINSTRUCTIONS:\n%s\nRECORDED EXPECTATIONS: %d\nDRAFT: %s\n"
                   "LAST RUN: %s\nAVAILABLE RELATED CASES: %s") % (
                sk["name"],
                "\n".join("%d. %s" % (i["n"], i["text"]) for i in sk["instructions"]),
                len(scope_items(sk["id"])), "yes" if sk.get("candidate") else "no",
                "yes" if _last_snap() else "no", json.dumps(available, ensure_ascii=False))
            convo = "\n".join("%s: %s" % (h.get("role"), h.get("text", "")[:400])
                               for h in hist[-8:])
            pending_manifest = pending_chat_manifest(sk)
            affirmative = is_affirmative(msg)
            if pending_manifest and (affirmative or ("确认" in msg and
                    any(word in msg for word in ("起草", "依据", "生成", "继续")))):
                return "draft", {"confirm": True}
            routed = ask(P_ROUTE, "%s\n\nCONVERSATION:\n%s\n\nMESSAGE: %s" % (
                ctx, convo, msg), 900) or {}
            return routed.get("capability") or "none", routed.get("args") or {}

        def is_affirmative(text):
            normalized = (text or "").strip("。！! ").lower()
            return normalized in ("同意", "没问题", "可以", "继续", "按这个来", "确认", "确认并继续",
                                  "生成候选", "按这个位置修改", "围绕这条指令生成修改",
                                  "从这里开始修改", "就改这条", "yes", "ok", "okay") or \
                ("确认" in normalized and any(word in normalized
                 for word in ("依据", "生成", "继续", "候选")))

        def record_decisions(decisions):
            recorded = []
            for decision in decisions:
                emit("record_expectation", {"case_id": decision["case_id"]})
                saved = op_expect(decision["commitment"], decision["disposition"],
                                  decision["case_id"])
                if saved.get("error"):
                    return recorded, saved
                recorded.append(decision)
            return recorded, None

        def build_preview_reply(prefix=""):
            emit("source_preview", {"label": "正在整理相关修改依据"})
            preview = ensure_repair_preview(sk)
            mark_repair_preview_presented(sk)
            body = repair_preview_text(sk, preview)
            return {"preview": public_repair_preview(sk)}, \
                ((prefix.rstrip() + "\n\n") if prefix else "") + body

        def continue_after_scope_edit(recorded):
            current = scope_readiness(sk)
            prefix = recorded_scope_text(recorded)
            if current.get("ready"):
                preview_result, preview_reply = build_preview_reply(prefix)
                automated, error = draft_and_compare()
                if error:
                    return {"recorded": recorded, "repair_preview": preview_result,
                            "automated": automated}, \
                        preview_reply + "\n\n候选检查没有完成：" + error.get("error", "未知错误")
                return {"recorded": recorded, "repair_preview": preview_result,
                        "automated": automated}, \
                    preview_reply + "\n\n" + candidate_review_text(sk, automated["compare"])
            emit("prepare_cases", {"label": "正在更新相关情况"})
            prepared = op_prepare_related_cases(commitment=_active_incident_commitment(sk))
            if prepared.get("error"):
                return {"recorded": recorded, "related_review": prepared}, \
                    prefix + "\n\n" + prepared["error"]
            missing = {row["case_id"] for row in current.get("missing") or []}
            remaining = [row for row in prepared.get("cases") or []
                         if row.get("case_id") in missing]
            return {"recorded": recorded, "remaining": remaining}, \
                prefix + "\n\n" + related_review_text(
                    remaining, "这些范围判断还需要重新确认。", prepared.get("intent"))

        def draft_and_compare():
            emit("draft", {"label": "正在根据你的判断生成候选"})
            drafted = self._draft_internal(auto=True, confirmation_message=msg)
            if drafted.get("error"):
                return {"draft": drafted}, drafted
            candidate = sk.get("candidate") or {}
            self._chunk({"type": "candidate", "candidate": {
                "name": candidate.get("name"), "version": candidate.get("version"),
                "instructions": copy.deepcopy(candidate.get("instructions") or []),
                "tools": [{"name": row.get("name"), "label": row.get("label"),
                           "signature": row.get("signature")}
                          for row in candidate.get("tools") or []],
            }})
            emit("compare", {"label": "正在检查修改前后的行为"})
            compared = op_compare()
            if compared.get("error"):
                return {"draft": drafted, "compare": compared}, compared
            record_semantic_event("comparison_viewed", sk, {
                "candidate_hash": (sk.get("candidate") or {}).get("content_hash"),
                "scope_version": sk.get("scope_version") or 1,
            }, chat_record.get("session", ""), "server-chat-response")
            return {"draft": drafted, "compare": compared}, None

        try:
            guided = False
            terminal = latest_task_end_review(sk.get("id"))
            readiness = scope_readiness(sk)
            if sk.get("scenario_id") and not terminal and not sk.get("candidate"):
                pack = scenario_rt.get_pack(sk["scenario_id"])
                incident_id = pack["entry_case"]
                missing_ids = {row["case_id"] for row in readiness.get("missing") or []}
                incident_context = _case_review_context(sk, incident_id)

                # First decision: the owner states the desired behavior for the
                # incident in ordinary language. On success, related executions
                # start immediately; no "list/open/run" commands are exposed.
                if incident_id in missing_ids and incident_context.get("run_count"):
                    parsed = extract_scope_decisions(msg, [incident_context])
                    if parsed["understood"]:
                        # This is a single owner-authored repair target. Preserve
                        # the exact utterance instead of letting the parser
                        # silently rewrite the policy that enters the manifest.
                        for decision in parsed["decisions"]:
                            if decision.get("case_id") == incident_id:
                                decision["commitment"] = msg
                        guided = True
                        recorded, error = record_decisions(parsed["decisions"])
                        if error:
                            result, reply = error, error["error"]
                        else:
                            incident_change = next((row.get("commitment") for row in recorded
                                                    if row.get("case_id") == incident_id), None)
                            emit("prepare_cases", {"label": "正在检查可能受影响的相关情况"})
                            prepared = op_prepare_related_cases(
                                commitment=incident_change,
                                before_case=lambda case: self._chunk({
                                    "type": "act", "capability": "prepare_case",
                                    "label": "正在检查：" + case.get("summary", "相关情况")}))
                            result = {"recorded": recorded, "related_review": prepared}
                            if prepared.get("error"):
                                reply = recorded_scope_text(recorded) + "\n\n" + prepared["error"]
                            else:
                                reply = recorded_scope_text(recorded) + "\n\n" + \
                                    related_review_text(prepared["cases"],
                                                        intent=prepared.get("intent"))

                # Second decision point: all neighbouring executions are shown
                # together and one reply can confirm/override every proposal.
                elif incident_id not in missing_ids and missing_ids:
                    contexts = related_case_contexts(sk)
                    evidence_ready = contexts and all(row.get("run_count") for row in contexts)
                    if not evidence_ready:
                        guided = True
                        emit("prepare_cases", {"label": "正在检查可能受影响的相关情况"})
                        prepared = op_prepare_related_cases(
                            before_case=lambda case: self._chunk({
                                "type": "act", "capability": "prepare_case",
                                "label": "正在检查：" + case.get("summary", "相关情况")}))
                        result = prepared
                        reply = (prepared.get("error") or
                                 related_review_text(prepared.get("cases") or [],
                                                     intent=prepared.get("intent")))
                    else:
                        parsed = extract_scope_decisions(msg, contexts)
                        if parsed["understood"]:
                            guided = True
                            decisions = [row for row in parsed["decisions"]
                                         if row["case_id"] in missing_ids]
                            recorded, error = record_decisions(decisions)
                            if error:
                                result, reply = error, error["error"]
                            else:
                                readiness = scope_readiness(sk)
                                if readiness["ready"]:
                                    result, reply = continue_after_scope_edit(recorded)
                                else:
                                    left = {row["case_id"] for row in readiness["missing"]}
                                    remaining = [row for row in contexts if row["case_id"] in left]
                                    result = {"recorded": recorded, "remaining": remaining}
                                    reply = recorded_scope_text(recorded) + "\n\n" + \
                                        related_review_text(remaining,
                                            "还有 %d 种情况需要你的判断。" % len(remaining),
                                            (current_scope_plan(sk) or {}).get("intent"))

                # Recovery path for a restored session that already has a full
                # pre-reveal scope but no candidate yet.
                elif readiness["ready"]:
                    guided = True
                    preview = current_repair_preview(sk)
                    if sk.get("scope_revision_required"):
                        contexts = [incident_context] + related_case_contexts(sk)
                        parsed = extract_scope_decisions(msg, contexts)
                        if parsed["understood"]:
                            recorded, error = record_decisions(parsed["decisions"])
                            if error:
                                result, reply = error, error["error"]
                            else:
                                result, reply = continue_after_scope_edit(recorded)
                        else:
                            result = {"scope_revision_required": True}
                            reply = "上一候选及其证据已经保留。请说明要修改哪一种情况的处理原则。"
                    else:
                        preview_result, preview_reply = build_preview_reply()
                        automated, error = draft_and_compare()
                        result = {"repair_preview": preview_result, "automated": automated}
                        reply = (preview_reply + "\n\n候选检查没有完成：" +
                                 error.get("error", "未知错误") if error else
                                 preview_reply + "\n\n" +
                                 candidate_review_text(sk, automated["compare"]))

            if sk.get("candidate") and not sk.get("last_compare") and not terminal and reply is None:
                guided = True
                emit("compare", {"label": "正在检查修改前后的行为"})
                compared = op_compare()
                result = {"compare": compared}
                reply = ("验证没有完成：" + compared.get("error", "未知错误")
                         if compared.get("error") else candidate_review_text(sk, compared))

            if not guided:
                cap, args = route_explicit_request()
                chat_record["capability"] = cap
                chat_record["args"] = copy.deepcopy(args)
                emit(cap)
                result = run_capability(cap, args)
                if cap == "record_expectation" and not result.get("error") and \
                        result.get("case_id") == (sk.get("scenario_id") and
                        scenario_rt.get_pack(sk["scenario_id"])["entry_case"]):
                    emit("prepare_cases", {"label": "正在检查可能受影响的相关情况"})
                    prepared = op_prepare_related_cases(
                        commitment=result.get("recorded"),
                        before_case=lambda case: self._chunk({
                            "type": "act", "capability": "prepare_case",
                            "label": "正在检查：" + case.get("summary", "相关情况")}))
                    result = {"recorded": result, "related_review": prepared}
                    reply = ("你的修复目标已经记录。\n\n" +
                             (prepared.get("error") or
                              related_review_text(prepared.get("cases") or [],
                                                  intent=prepared.get("intent"))))
                elif cap == "edit_instruction" and not result.get("error"):
                    emit("compare", {"label": "正在检查修改后的行为"})
                    compared = op_compare()
                    result = {"edit": result, "compare": compared}
                    reply = ("修改已保存，但验证没有完成：" + compared.get("error", "未知错误")
                             if compared.get("error") else candidate_review_text(sk, compared))
                elif cap == "decide" and result.get("decided") == "publish":
                    questionnaire_path = result.get("questionnaire_path")
                    reply = ("发布成功。新版本 v%s 已经生效，本轮执行证据和发布理由均已保存。"
                             "接下来请完成任务问卷。" % result.get("version"))
                elif cap == "decide" and result.get("decided") == "defer":
                    questionnaire_path = result.get("questionnaire_path")
                    reply = "已暂缓本次发布，当前版本保持不变，原因已经保存。接下来请完成任务问卷。"
                elif cap == "decide" and result.get("decided") == "revise":
                    automated, error = draft_and_compare()
                    result = {"revision": result, "automated": automated}
                    reply = ("重新起草没有完成：" + error.get("error", "未知错误") if error else
                             candidate_review_text(sk, automated["compare"]))
                elif cap == "decide" and result.get("decided") == "gather":
                    compared = result.get("compare") or {}
                    reply = ("补充检查没有完成：" + compared.get("error", "未知错误")
                             if compared.get("error") else candidate_review_text(sk, compared))
                elif cap == "reopen_scope" and not result.get("error"):
                    reply = ("上一候选、修改前后结果和发布判断已作为上一轮记录保留。"
                             "请说明这一轮要修改哪一种情况的处理原则。")
                elif cap == "check_candidate_block" and not result.get("error"):
                    reply = "%s\n\n%s" % (result.get("interpretation"), result.get("limitation"))
                else:
                    narrated = ask(P_NARRATE,
                        "CAPABILITY: %s\n\nRESULT:\n%s\n\nUSER MESSAGE: %s" % (
                            cap, json.dumps(result, ensure_ascii=False)[:4000], msg), 1200)
                    reply = (narrated or {}).get("reply") or \
                        json.dumps(result, ensure_ascii=False)[:400]
            else:
                chat_record["capability"] = "guided_review"
                chat_record["args"] = {"automatic": True}
        except Exception as e:  # noqa: BLE001
            result = {"error": "%s: %s" % (type(e).__name__, str(e)[:180])}
            reply = "这一步没有完成：%s。你刚才的消息已经保留，可以直接重试。" % result["error"]

        progress = chat_progress(sk, chat_record["session"])
        if not questionnaire_path and not guided and progress.get("next_action") and \
                progress["next_action"] not in (reply or "") and not sk.get("last_compare"):
            reply = (reply or "").rstrip() + "\n\n" + chat_guidance_text(progress)
        chat_record["result"] = copy.deepcopy(result)
        chat_record["reply"] = reply
        chat_record["progress"] = copy.deepcopy(progress)
        self._chunk({"type": "text", "text": reply})
        if questionnaire_path:
            self._chunk({"type": "task_complete", "path": questionnaire_path})
        self._close()

    def _draft_internal(self, confirm=False, auto=False, confirmation_message=None):
        sk = cur()
        readiness = scope_readiness(sk)
        if not readiness["ready"]:
            return {
                "error": "起草前还需要处理所有已提供的产品内情况。",
                "missing_cases": readiness["missing"],
                "next_action": "先打开缺少的情况并记录你的判断，再请求起草。",
            }
        if sk.get("scope_revision_required"):
            return {"error": "请先说明并保存本轮要调整的范围。"}
        try:
            preview = ensure_repair_preview(sk)
        except ValueError as error:
            return {"error": str(error)}
        if auto:
            mark_repair_preview_presented(sk)
            manifest = compile_manifest(sk, "chat")
            manifest["confirmed_at"] = time.time()
            manifest["commitment_source"] = "owner-authored-scope"
            manifest["candidate_request_message"] = (confirmation_message or "")[:1000]
            confirm_repair_preview(sk, "chat-candidate-input")
        elif not confirm:
            manifest = compile_manifest(sk, "chat")
            return {
                "requires_confirmation": True,
                "manifest_id": manifest["id"],
                "scope_version": manifest["scope_version"],
                "visible_commitments": [{"disposition": row["disposition"],
                                           "text": row["commitment"]}
                                          for row in manifest["visible_commitments"]],
                "withheld_commitments": [{"disposition": row["disposition"],
                                            "text": row["commitment"]}
                                           for row in manifest["withheld_commitments"]],
                "source_evidence_count": len(manifest["source_evidence"]),
                "repair_preview": public_repair_preview(sk, preview),
                "instruction_count": len(sk["instructions"]),
                "next_action": "这些依据可随时回看；候选生成只采用已经确认的适用范围。",
            }
        else:
            manifest = pending_chat_manifest(sk)
            if not manifest:
                return {"error": "还没有可用的候选起草记录，请重新生成候选。"}
            manifest["confirmed_at"] = time.time()
            confirm_repair_preview(sk, "chat-candidate-input")
        exps = [{"id": x["id"], "expectation": x["commitment"],
                 "disposition": x["disposition"], "task": x.get("task", "")}
                for x in manifest["visible_commitments"]]
        selected_ids = {row.get("id") for row in manifest.get("source_evidence") or []}
        ev = [{"id": p["id"], "instruction": p["n"], "probe": p["kind"],
               "changed": p["changed"], "confidence": p.get("confidence"),
               "note": p.get("note")}
              for p in mine(STATE["probes"]) if p.get("n") and p.get("id") in selected_ids]
        feedback = copy.deepcopy(sk.pop("pending_candidate_feedback", []))
        out = generate_candidate(sk["instructions"], exps, ev, feedback)
        if not out or "instructions" not in out:
            return out or {"error": "起草失败"}
        cand = candidate_record(sk, out, "ai", manifest)
        with LOCK:
            sk["candidate"] = cand
        record_semantic_event("candidate_revealed", sk, {
            "candidate_hash": cand.get("content_hash"),
            "scope_version": sk.get("scope_version") or 1,
        })
        return {"drafted": True, "instructions": cand["instructions"]}

    def _decide_internal(self, action, reason):
        sk = cur()
        if action == "publish":
            if not sk.get("candidate"):
                return {"error": "没有草稿"}
            compared = sk.get("last_compare") or []
            outcome = [{
                "situation_id": row.get("situation_id"),
                "case_id": row.get("case_id"),
                "expectation": row["expectation"],
                "disposition": row["disposition"],
                "conflict": row["verdict"] in ("unmet", "broken"),
                "needs_judgment": row["verdict"] == "needs_judgment",
                "insufficient": row["verdict"] == "insufficient",
            } for row in compared]
            problem = release_readiness(sk, outcome, reason)
            if problem:
                return {"error": problem}
            candidate, _ = publish_candidate(sk, reason, outcome, "chat")
            return {"decided": action, "reason": reason, "version": candidate["version"],
                    "questionnaire_path": "/questionnaire"}
        if action == "revise":
            revised = begin_candidate_revision(sk, "chat-owner-requested-candidate-revision")
            if revised.get("error"):
                return revised
            return {"decided": action, "reason": reason, "revision": revised}
        if action == "gather":
            compared = op_compare(GATHER_RUNS)
            return {"decided": action, "reason": reason, "compare": compared,
                    "runs_per_artifact": GATHER_RUNS}
        compared = sk.get("last_compare") or []
        outcome = [{
            "situation_id": row.get("situation_id"), "case_id": row.get("case_id"),
            "expectation": row.get("expectation"), "disposition": row.get("disposition"),
            "conflict": row.get("verdict") in ("unmet", "broken"),
            "needs_judgment": row.get("verdict") == "needs_judgment",
            "insufficient": row.get("verdict") == "insufficient",
            "execution_failed": bool(row.get("execution_failed")),
        } for row in compared]
        rec = {"id": nid("v"), "skill": STATE["active"], "action": action, "reason": reason,
               "at": time.time(), "skill_hash": sk["hash"],
               "scope_version": sk.get("scope_version") or 1,
               "condition": "chat",
               "situations": [{"id": x.get("id"), "commitment": x["commitment"],
                               "disposition": x["disposition"], "sealed": bool(x.get("sealed"))}
                              for x in scope_items(sk["id"])],
               "outcome": outcome,
               "evidence": (release_evidence(sk, sk.get("candidate"))
                            if sk.get("candidate") and compared else {})}
        with LOCK:
            STATE.setdefault("reviews", []).append(rec)
        result = {"decided": action, "reason": reason}
        if action == "defer":
            record_semantic_event("decision_submitted", sk, {
                "review_id": rec["id"], "action": "defer",
                "candidate_hash": (sk.get("candidate") or {}).get("content_hash"),
            }, (sk.get("study_context") or {}).get("session", ""))
            record_semantic_event("task_completed", sk, {
                "review_id": rec["id"], "action": "defer",
            }, (sk.get("study_context") or {}).get("session", ""))
            result["questionnaire_path"] = "/questionnaire"
        return result

    def h_api_repair_preview(self):
        """Build the automatic M0 source-location preview for the active scope."""
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        try:
            preview = ensure_repair_preview(sk)
        except ValueError as error:
            return self._json({"error": str(error)}, 409)
        mark_repair_preview_presented(sk)
        return self._json({"preview": public_repair_preview(sk)})

    def h_api_manifest(self):
        """Materialize candidate inputs after the owner commits the behavior scope."""
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        readiness = scope_readiness(sk)
        if not readiness["ready"]:
            return self._json({
                "error": "起草前还需要处理所有已提供的产品内情况。",
                "missing_cases": readiness["missing"],
            }, 409)
        if sk.get("scope_revision_required"):
            return self._json({"error": "请先保存本轮范围调整，再生成候选修改。"}, 409)
        try:
            ensure_repair_preview(sk)
        except ValueError as error:
            return self._json({"error": str(error)}, 409)
        self._body()
        manifest = compile_manifest(sk, "workspace")
        return self._json({
            "id": manifest["id"], "hash": manifest["hash"],
            "scope_version": manifest["scope_version"],
            "scope_hash": manifest["scope_hash"],
            "skill_hash": manifest["skill_hash"],
            "repair_preview_id": manifest.get("repair_preview_id"),
            "repair_preview_hash": manifest.get("repair_preview_hash"),
            "review_round": manifest.get("review_round"),
            "visible": {
                "expectations": [{"id": x["id"], "text": x["commitment"],
                                  "disposition": x["disposition"]}
                                 for x in manifest["visible_commitments"]],
                "probes": len(manifest["source_evidence"]),
                "source_evidence": copy.deepcopy(manifest["source_evidence"]),
                "instructions": len(sk["instructions"])},
            "withheld": {
                "expectations": [{"id": x["id"], "text": x["commitment"],
                                  "disposition": x["disposition"]}
                                 for x in manifest["withheld_commitments"]]},
            "excluded": [{"case_id": x.get("case_id"), "task": x.get("task")}
                         for x in manifest.get("excluded_cases") or []],
            "limitations": [
                "建议位置只用于选择修改起点；候选是否有效将在完整情况检查中判断。",
                "产品内情况之外的行为不在本次发布证据范围内。",
            ]})

    def h_api_draft(self):
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        b = self._body()
        manifest = find_manifest(sk, b.get("manifest_id"))
        if not manifest:
            return self._json({"error": "候选起草记录不存在，请重新生成候选"}, 409)
        if manifest.get("scope_version") != (sk.get("scope_version") or 1) or \
                manifest.get("skill_hash") != sk.get("hash"):
            return self._json({"error": "适用范围已经变化，请重新生成候选"}, 409)
        if manifest.get("repair_preview_id") != sk.get("active_repair_preview_id"):
            return self._json({"error": "相关修改依据已经变化，请重新生成候选"}, 409)
        manifest["confirmed_at"] = time.time()
        manifest["commitment_source"] = "owner-authored-scope"
        confirm_repair_preview(sk, "workspace-candidate-input")
        exps = [{"id": s["id"], "expectation": s["commitment"],
                 "disposition": s["disposition"], "task": s.get("task", "")}
                for s in manifest["visible_commitments"]]
        fb = b.get("feedback") or []
        selected_ids = {row["id"] for row in manifest["source_evidence"]}
        ev = [{"id": p["id"], "instruction": p["n"], "probe": p["kind"],
               "changed": p["changed"], "confidence": p.get("confidence"), "note": p["note"]}
              for p in mine(STATE["probes"]) if p.get("n") and p.get("id") in selected_ids]
        out = generate_candidate(sk["instructions"], exps, ev, fb)
        if not out or "instructions" not in out:
            return self._json(out or {"error": "起草失败"}, 500)
        cand = candidate_record(sk, out, "ai", manifest)
        with LOCK:
            sk["candidate"] = cand
        record_semantic_event("candidate_revealed", sk, {
            "candidate_hash": cand.get("content_hash"),
            "scope_version": sk.get("scope_version") or 1,
        })
        return self._json({"candidate": cand})

    def h_api_edit(self):
        """用户直接改写指令，生成或更新候选版本。"""
        b = self._body()
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        ins = b.get("instructions")
        if not ins:
            return self._json({"error": "内容为空"}, 400)
        try:
            cand = save_owner_candidate(sk, ins)
        except ValueError as error:
            return self._json({"error": str(error)}, 409)
        return self._json({"candidate": cand})

    def h_api_candidate_revise(self):
        """Archive the revealed candidate and redraft against the same locked scope."""
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        body = self._body()
        if body.get("outcome"):
            sk["last_compare"] = comparison_from_outcome(body.get("outcome"))
        result = begin_candidate_revision(sk, "owner-requested-candidate-revision")
        return self._json(result, 409 if result.get("error") else 200)

    def h_api_scope_reopen(self):
        """Archive candidate evidence and begin an explicitly post-reveal scope round."""
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        body = self._body()
        if body.get("outcome"):
            sk["last_compare"] = comparison_from_outcome(body.get("outcome"))
        result = begin_scope_revision(sk, "owner-requested-scope-revision")
        return self._json(result, 409 if result.get("error") else 200)

    def h_api_decide(self):
        """记录一次评审决定：发布 / 重新起草 / 补充执行 / 暂缓。"""
        b = self._body()
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        rec = {"id": nid("v"), "skill": STATE["active"], "action": b.get("action"),
               "reason": (b.get("reason") or "").strip(),
               "at": time.time(), "skill_hash": sk["hash"],
               "scope_version": sk.get("scope_version") or 1,
               "candidate_hash": (sk.get("candidate") or {}).get("hash"),
               "condition": b.get("condition") or "workspace",
               "situations": [{"id": x.get("id"), "commitment": x["commitment"],
                               "disposition": x["disposition"], "sealed": bool(x.get("sealed"))}
                              for x in scope_items(sk["id"])],
               "outcome": b.get("outcome") or []}
        if rec["action"] == "defer" and sk.get("candidate"):
            rec["evidence"] = release_evidence(sk, sk["candidate"])
        with LOCK:
            STATE.setdefault("reviews", []).append(rec)
        response = {"review": rec}
        if rec["action"] == "defer":
            record_semantic_event("decision_submitted", sk, {
                "review_id": rec["id"], "action": "defer",
                "candidate_hash": (sk.get("candidate") or {}).get("content_hash"),
            })
            record_semantic_event("task_completed", sk, {
                "review_id": rec["id"], "action": "defer",
            })
            response["questionnaire_path"] = "/questionnaire"
        return self._json(response)

    def h_api_blockprobe(self):
        """对候选的某条指令做临时移除，估计该改动块的影响范围。
        基线是候选本身，与对原 skill 的定位证据分属不同类型。"""
        b = self._body()
        snap = STATE["snapshots"].get(b.get("snapshot"))
        if not snap or not cur() or not cur().get("candidate"):
            return self._json({"error": "需要先有草稿"}, 400)
        self._open()
        self._chunk({"type": "start", "n": b.get("n"), "baseline": "candidate"})
        result = op_block_probe(b.get("n"), snap.get("case_id"), b.get("k", BLOCK_PROBE_RUNS))
        if result.get("error"):
            self._chunk({"type": "error", "error": result["error"]})
        else:
            self._chunk({"type": "done", "probe": result["probe"],
                         "code": result["code"],
                         "interpretation": result.get("interpretation"),
                         "limitation": result.get("limitation")})
        self._close()

    def h_api_publish(self):
        b = self._body()
        sk = cur()
        problem = release_readiness(sk, b.get("outcome") or [], b.get("reason") or "")
        if problem:
            return self._json({"error": problem}, 409)
        candidate, review = publish_candidate(sk, b.get("reason") or "",
                                              b.get("outcome") or [], "workspace")
        return self._json({"skill": candidate, "review": review,
                           "questionnaire_path": "/questionnaire"})

    def h_api_discard(self):
        sk = cur()
        if sk:
            archive_candidate_round(sk, "owner-discarded-candidate")
            with LOCK:
                sk["candidate"] = None
                sk["last_compare"] = None
                sk["last_compare_budget"] = None
        return self._json({"ok": True})


def main():
    if not API_KEY:
        print("!! 未设置 DEEPSEEK_API_KEY", file=sys.stderr)
    if RESUME_FILE:
        load_resume_file()
    print("SkillScope  http://127.0.0.1:%d   模型 %s" % (PORT, MODEL))
    if STUDY_DATA_DIR:
        print("研究记录目录 %s" % STUDY_DATA_DIR, file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
