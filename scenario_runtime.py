#!/usr/bin/env python3
"""Executable, resettable tool worlds used by SkillScope scenario packs.

The agent never receives a scenario's oracle or complete fixture.  It sees JSON
Schemas for tools and obtains data only by calling them. Every execution gets
an isolated copy of ``world`` so reads and state-changing tool calls are reproducible.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


HERE = Path(__file__).parent
PACK_ROOT = HERE / "scenarios" / "packs"
PACK_SCHEMA = "skillscope/scenario-pack/1"
RUNTIME_SCHEMA = "skillscope/tool-world/1"
PARTICIPANT_HIDDEN_ROLES = frozenset(("prediction-holdout", "research-holdout"))
WORK_ORDER_TEXT_FIELDS = (
    "id", "label", "title", "role", "context", "objective", "completion",
    "environment_note",
)


def canonical_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _skill_instructions(text):
    rows = []
    for line in text.splitlines():
        match = re.match(r"^\s*(\d+)[.、]\s*(.+?)\s*$", line)
        if match:
            rows.append({"n": int(match.group(1)), "text": match.group(2)})
    if not rows:
        raise ValueError("scenario skill contains no numbered instructions")
    return rows


def load_packs(root=PACK_ROOT):
    packs = {}
    if not root.exists():
        return packs
    for manifest_path in sorted(root.glob("*/scenario.json")):
        pack = _read_json(manifest_path)
        if pack.get("schema") != PACK_SCHEMA:
            raise ValueError("unsupported scenario schema in %s" % manifest_path)
        pack = copy.deepcopy(pack)
        pack["_dir"] = str(manifest_path.parent)
        skill_path = manifest_path.parent / pack["skill"]["faulty"]
        reference_path = manifest_path.parent / pack["skill"]["reference"]
        skill_text = skill_path.read_text(encoding="utf-8")
        reference_text = reference_path.read_text(encoding="utf-8")
        pack["skill"]["faulty_text"] = skill_text
        pack["skill"]["faulty_instructions"] = _skill_instructions(skill_text)
        pack["skill"]["reference_text"] = reference_text
        pack["skill"]["reference_instructions"] = _skill_instructions(reference_text)
        case_ids = [case["id"] for case in pack.get("cases", [])]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("duplicate case id in %s" % manifest_path)
        if pack.get("entry_case") not in case_ids:
            raise ValueError("entry_case missing in %s" % manifest_path)
        for case in pack.get("cases", []):
            if not case.get("oracle"):
                continue
            calibration = case.get("calibration_expectation") or {}
            if calibration.get("faulty") not in ("pass", "fail") or \
                    calibration.get("reference") not in ("pass", "fail"):
                raise ValueError(
                    "oracle case %s needs explicit faulty/reference calibration in %s" %
                    (case.get("id"), manifest_path))
        prediction_cases = [case for case in pack.get("cases", [])
                            if case.get("role") == "prediction-holdout"]
        if len(prediction_cases) != 2:
            raise ValueError("scenario must contain exactly two prediction holdouts in %s" %
                             manifest_path)
        question_ids = []
        for case in prediction_cases:
            questions = (case.get("study_measure") or {}).get("prediction_questions") or []
            if len(questions) != 3:
                raise ValueError("each prediction holdout must contain three questions in %s" %
                                 manifest_path)
            question_ids.extend(row.get("id") for row in questions)
        if len(question_ids) != len(set(question_ids)) or any(not value for value in question_ids):
            raise ValueError("prediction question ids must be nonempty and unique in %s" %
                             manifest_path)
        research_cases = [case for case in pack.get("cases", [])
                          if case.get("role") == "research-holdout"]
        if len(research_cases) != 1:
            raise ValueError("scenario must contain exactly one research holdout in %s" %
                             manifest_path)
        work_order = pack.get("work_order") or {}
        missing_work_order = [key for key in WORK_ORDER_TEXT_FIELDS
                              if not isinstance(work_order.get(key), str)
                              or not work_order.get(key).strip()]
        if missing_work_order:
            raise ValueError("work_order missing %s in %s" % (
                ", ".join(missing_work_order), manifest_path))
        if not isinstance(work_order.get("time_minutes"), int) \
                or work_order["time_minutes"] <= 0:
            raise ValueError("work_order time_minutes must be positive in %s" % manifest_path)
        review_steps = work_order.get("review_steps") or []
        if not isinstance(review_steps, list) or not (2 <= len(review_steps) <= 5) or \
                any(not isinstance(row, str) or not row.strip() for row in review_steps):
            raise ValueError("work_order review_steps must contain 2-5 strings in %s" %
                             manifest_path)
        pack["pack_hash"] = canonical_hash({
            "manifest": {k: v for k, v in pack.items() if not k.startswith("_")
                         and k != "pack_hash"},
            "faulty_skill": skill_text,
            "reference_skill": reference_text,
        })
        packs[pack["id"]] = pack
    return packs


PACKS = load_packs()


def public_work_order(pack):
    """Return the participant-facing assignment without scenario answers.

    The allowlist keeps repair suggestions, reference artifacts, cases, and
    hidden oracles on the researcher side. ``task_hash`` versions the exact
    brief participants acknowledged so study exports can reconstruct it.
    """
    source = pack.get("work_order") or {}
    order = {key: source[key] for key in WORK_ORDER_TEXT_FIELDS}
    order["time_minutes"] = source["time_minutes"]
    order["review_steps"] = copy.deepcopy(source.get("review_steps") or [])
    order["scenario_id"] = pack["id"]
    order["task_hash"] = canonical_hash(order)[:12]
    return order


def public_scenarios():
    return [{
        "id": pack["id"],
        "title": pack["title"],
        "description": pack["description"],
        "domain": pack["domain"],
        "entry_case": pack["entry_case"],
        "instruction_count": len(pack["skill"]["faulty_instructions"]),
        "tool_count": len(pack.get("tools", [])),
        "pack_hash": pack["pack_hash"][:12],
        "work_order": public_work_order(pack),
    } for pack in PACKS.values()]


def get_pack(pack_id):
    pack = PACKS.get(pack_id)
    if not pack:
        raise KeyError("unknown scenario: %s" % pack_id)
    return pack


def get_case(pack, case_id):
    for case in pack.get("cases", []):
        if case["id"] == case_id:
            return case
    raise KeyError("unknown case: %s/%s" % (pack["id"], case_id))


def participant_hidden(case):
    """Whether a case must stay out of the repair UI and candidate context."""
    return case.get("role") in PARTICIPANT_HIDDEN_ROLES


def skill_record(pack):
    tools = []
    sources = {}
    for tool in pack.get("tools", []):
        item = {
            "name": tool["name"],
            "api_name": tool["api_name"],
            "label": tool["label"],
            "signature": tool.get("signature", ""),
            "returns": tool.get("returns", ""),
            "kind": tool["kind"],
            "side_effecting": tool["kind"] == "write",
            "description": tool["description"],
            "parameters": copy.deepcopy(tool["parameters"]),
        }
        tools.append(item)
        sources[tool["name"]] = {
            "label": tool["label"],
            "kind": tool["kind"],
            "signature": tool.get("signature", ""),
            "returns": tool.get("returns", ""),
            "rows": [],
            "api_name": tool["api_name"],
            "parameters": copy.deepcopy(tool["parameters"]),
        }
    entry = get_case(pack, pack["entry_case"])
    fixtures = entry.get("world", {}).get("fixtures", {})
    for tool in pack.get("tools", []):
        value = fixtures.get(tool["api_name"])
        if tool["kind"] != "write" and value is not None:
            sources[tool["name"]]["rows"] = copy.deepcopy(
                value if isinstance(value, list) else [value])
    return {
        "name": pack["skill"]["name"],
        "instructions": copy.deepcopy(pack["skill"]["faulty_instructions"]),
        "tools": tools,
        "sources": sources,
        "config": {
            "scenario_id": pack["id"],
            "scenario_pack_hash": pack["pack_hash"],
            "clock": pack["clock"],
            "execution_runtime": RUNTIME_SCHEMA,
        },
        "scenario_id": pack["id"],
        "scenario_pack_hash": pack["pack_hash"],
        "work_order": public_work_order(pack),
    }


def case_snapshot(pack, case_id):
    case = get_case(pack, case_id)
    world = copy.deepcopy(case["world"])
    world.setdefault("state", {})
    world.setdefault("fixtures", {})
    world["clock"] = pack["clock"]
    preview = {}
    for tool in pack.get("tools", []):
        value = world["fixtures"].get(tool["api_name"])
        if tool["kind"] != "write" and value is not None:
            preview[tool["name"]] = copy.deepcopy(value)
    public_case = {
        "id": case["id"],
        "role": case["role"],
        "source": case.get("source", "generated"),
        "task": case["task"],
        "summary": case["summary"],
        "changed_factors": copy.deepcopy(case.get("changed_factors", [])),
        "parent_case": case.get("parent_case"),
    }
    snapshot = {
        "task": case["task"],
        "tools": preview,
        "args": {},
        "missing": [],
        "summary": case["summary"],
        "fact_schema": copy.deepcopy(pack.get("fact_schema", [])),
        "recorded": None,
        "source": case.get("source", "generated"),
        "changed_factors": copy.deepcopy(case.get("changed_factors", [])),
        "runtime": RUNTIME_SCHEMA,
        "scenario_id": pack["id"],
        "scenario_pack_hash": pack["pack_hash"],
        "case_id": case["id"],
        "case_role": case["role"],
        "case_hash": canonical_hash(public_case),
        "world": world,
        "world_hash": canonical_hash(world),
        "tool_schema_hash": canonical_hash(pack.get("tools", [])),
        "clock": pack["clock"],
    }
    # Related-case review metadata helps the owner classify a boundary, but an
    # incident's problem statement must be derived from a completed execution.
    if case.get("review_prompt") is not None:
        snapshot["review_prompt"] = copy.deepcopy(case["review_prompt"])
    return snapshot


def neighbouring_cases(pack, base_case_id):
    rows = []
    for item in get_case(pack, base_case_id).get("neighbours", []):
        case = get_case(pack, item["case_id"])
        rows.append({
            "case_id": case["id"],
            "text": case["summary"],
            "suggest": item["suggest"],
            "why": item["why"],
            "relation_type": item.get("relation_type", "related-case"),
            "intent_link": item.get("intent_link") or item["why"],
            "exposure": item.get("exposure", "author-visible"),
            "changed_factors": copy.deepcopy(case.get("changed_factors", [])),
            "review_prompt": copy.deepcopy(case.get("review_prompt")),
        })
    return rows


def tool_definitions(pack):
    return [{
        "type": "function",
        "function": {
            "name": tool["api_name"],
            "description": "%s（产品中显示为 %s）" % (tool["description"], tool["name"]),
            "parameters": copy.deepcopy(tool["parameters"]),
        },
    } for tool in pack.get("tools", [])]


def _tool(pack, api_name):
    for tool in pack.get("tools", []):
        if tool["api_name"] == api_name or tool["name"] == api_name:
            return tool
    return None


def _rows(world, api_name):
    value = world.get("fixtures", {}).get(api_name, [])
    return value if isinstance(value, list) else [value]


def _between(value, lower=None, upper=None):
    if lower not in (None, "") and str(value) < str(lower):
        return False
    if upper not in (None, "") and str(value) > str(upper):
        return False
    return True


def _travel_calendar(world, args):
    lower, upper = args.get("from"), args.get("to")
    return [row for row in _rows(world, "calendar_commitments")
            if _between(row.get("start", ""), lower, upper)]


def _travel_search(world, args):
    rows = _rows(world, "flights_search")
    result = []
    for row in rows:
        if args.get("origin") and row.get("origin") != args["origin"]:
            continue
        if args.get("destination") and row.get("destination") != args["destination"]:
            continue
        if args.get("after") and row.get("depart", "") < args["after"]:
            continue
        result.append(row)
    return result


def _travel_confirmation(world, args):
    state = world.setdefault("state", {})
    sequence = len(state.setdefault("confirmations", [])) + 1
    record = {
        "id": "CONF-%03d" % sequence,
        "action": args.get("action"),
        "amount": args.get("amount"),
        "approved": bool(state.get("confirmation_response", True)),
    }
    state["confirmations"].append(record)
    return record


def _travel_rebook(world, args):
    state = world.setdefault("state", {})
    flight = next((row for row in _rows(world, "flights_search")
                   if row.get("id") == args.get("flight_id")), None)
    if not flight:
        return {"ok": False, "error": "unknown flight_id"}
    confirmation_id = args.get("confirmation_id") or ""
    confirmed = any(row.get("id") == confirmation_id and row.get("approved")
                    for row in state.get("confirmations", []))
    record = {
        "flight_id": flight["id"],
        "confirmation_id": confirmation_id or None,
        "confirmation_recorded": confirmed,
        "status": "booked",
    }
    state.setdefault("booking_attempts", []).append(record)
    return {"ok": True, **record}


def _lookup(world, api_name, args):
    rows = _rows(world, api_name)
    for row in rows:
        matches = True
        for key, value in args.items():
            if value in (None, ""):
                continue
            if key in row and str(row[key]) != str(value):
                matches = False
                break
        if matches:
            return row
    return None


def _manager_approval(world, args):
    state = world.setdefault("state", {})
    sequence = len(state.setdefault("approvals", [])) + 1
    record = {
        "id": "APR-%03d" % sequence,
        "receipt_id": args.get("receipt_id"),
        "reason": args.get("reason"),
        "approved": bool(state.get("manager_response", True)),
    }
    state["approvals"].append(record)
    return record


def _record_decision(world, args):
    record = {
        "receipt_id": args.get("receipt_id"),
        "decision": args.get("decision"),
        "reason": args.get("reason"),
        "status": "recorded",
    }
    world.setdefault("state", {}).setdefault("decisions", []).append(record)
    return {"ok": True, **record}


def _ledger_post(world, args):
    state = world.setdefault("state", {})
    record = {
        "receipt_id": args.get("receipt_id"),
        "approval_id": args.get("approval_id") or None,
        "status": "posted",
    }
    state.setdefault("ledger_entries", []).append(record)
    return {"ok": True, "entry_id": "LEDGER-%03d" % len(state["ledger_entries"]), **record}


OPERATIONS = {
    "travel_calendar": lambda world, args, _: _travel_calendar(world, args),
    "travel_search": lambda world, args, _: _travel_search(world, args),
    "travel_confirmation": lambda world, args, _: _travel_confirmation(world, args),
    "travel_rebook": lambda world, args, _: _travel_rebook(world, args),
    "lookup": lambda world, args, api_name: _lookup(world, api_name, args),
    "manager_approval": lambda world, args, _: _manager_approval(world, args),
    "record_decision": lambda world, args, _: _record_decision(world, args),
    "ledger_post": lambda world, args, _: _ledger_post(world, args),
}


def validate_arguments(parameters, arguments):
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    required = parameters.get("required", [])
    missing = [name for name in required if name not in arguments]
    if missing:
        return "missing required arguments: %s" % ", ".join(missing)
    props = parameters.get("properties", {})
    extra = [name for name in arguments if name not in props]
    if extra and parameters.get("additionalProperties") is False:
        return "unknown arguments: %s" % ", ".join(extra)
    for name, value in arguments.items():
        schema = props.get(name, {})
        expected = schema.get("type")
        good = (expected == "string" and isinstance(value, str)) or \
               (expected == "boolean" and isinstance(value, bool)) or \
               (expected == "integer" and isinstance(value, int) and not isinstance(value, bool)) or \
               (expected == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)) or \
               (expected == "array" and isinstance(value, list)) or \
               (expected == "object" and isinstance(value, dict)) or expected is None
        if not good:
            return "%s must be %s" % (name, expected)
        if schema.get("enum") and value not in schema["enum"]:
            return "%s must be one of %s" % (name, schema["enum"])
    return None


def dispatch(pack, world, api_name, arguments):
    tool = _tool(pack, api_name)
    if not tool:
        return {"ok": False, "error": "unknown tool: %s" % api_name}
    error = validate_arguments(tool["parameters"], arguments)
    if error:
        return {"ok": False, "error": error}
    operation = OPERATIONS.get(tool["operation"])
    if not operation:
        return {"ok": False, "error": "unsupported operation"}
    value = operation(world, arguments, tool["api_name"])
    return {"ok": True, "data": value}


def tool_step(pack, api_name, arguments, result, instructions):
    tool = _tool(pack, api_name) or {"name": api_name, "label": api_name,
                                    "kind": "query", "operation": "unknown"}
    if tool.get("operation") in ("travel_confirmation", "manager_approval"):
        kind = "gate"
    elif tool.get("operation") == "record_decision":
        kind = "decision"
    elif tool["kind"] == "write":
        kind = "action"
    else:
        kind = "tool"
    source_instruction = None
    needles = {tool["name"].lower(), api_name.lower(), tool["label"].lower()}
    for instruction in instructions:
        text = instruction["text"].lower()
        if any(needle in text for needle in needles):
            source_instruction = instruction["n"]
            break
    data = result.get("data") if isinstance(result, dict) else result
    if isinstance(data, list):
        detail = "返回 %d 条记录" % len(data)
    elif isinstance(data, dict) and data.get("error"):
        detail = str(data["error"])[:40]
    elif tool.get("operation") == "travel_rebook":
        detail = "改签已完成"
    elif tool.get("operation") == "record_decision":
        detail = "审核结论已记录"
    elif tool.get("operation") == "ledger_post":
        detail = "财务凭证已创建"
    elif tool["kind"] == "write":
        detail = "操作已完成"
    else:
        detail = "已返回结构化结果"
    return {
        "kind": kind,
        "title": tool["label"][:16],
        "detail": detail,
        "from": source_instruction,
        "tool": tool["name"],
        "api_name": tool.get("api_name", api_name),
        "args": copy.deepcopy(arguments),
        "result": copy.deepcopy(result),
    }


def _travel_facts(world, trace):
    state = world.get("state", {})
    booking = (state.get("booking_attempts") or [None])[-1]
    flight = None
    if booking:
        flight = next((row for row in _rows(world, "flights_search")
                       if row.get("id") == booking.get("flight_id")), None)
    confirmations = state.get("confirmations", [])
    booking_ix = next((i for i, row in enumerate(trace)
                       if row.get("api_name") == "booking_rebook"), None)
    confirmation_ix = next((i for i, row in enumerate(trace)
                            if row.get("api_name") == "traveler_request_confirmation"), None)
    deadline = state.get("required_arrival_before")
    return {
        "selected_flight": flight.get("id") if flight else None,
        "arrival_time": flight.get("arrive") if flight else None,
        "price": flight.get("price") if flight else None,
        "confirmation_requested": bool(confirmations),
        "confirmation_before_booking": bool(
            confirmation_ix is not None and booking_ix is not None and confirmation_ix < booking_ix),
        "booking_completed": bool(booking),
        "on_time_for_commitment": bool(
            flight and (not deadline or flight.get("arrive", "") <= deadline)),
        "tool_call_count": len(trace),
    }


def _expense_facts(world, trace):
    state = world.get("state", {})
    decision = (state.get("decisions") or [None])[-1]
    receipt_id = decision.get("receipt_id") if decision else state.get("target_receipt")
    receipt = next((row for row in _rows(world, "receipts_get")
                    if row.get("receipt_id") == receipt_id), None)
    profile = None
    if receipt:
        profile = next((row for row in _rows(world, "hr_profile")
                        if row.get("employee_id") == receipt.get("employee_id")), None)
    required = ("invoice_no", "date", "amount", "currency", "merchant")
    complete = bool(receipt and all(receipt.get(key) not in (None, "") for key in required))
    restrictions = (profile or {}).get("restricted_categories", [])
    if isinstance(restrictions, str):
        restrictions = [part.strip() for part in restrictions.split(",") if part.strip()]
    return {
        "receipt_id": receipt_id,
        "decision": decision.get("decision") if decision else None,
        "decision_recorded": bool(decision),
        "ledger_entry_created": bool(state.get("ledger_entries")),
        "manager_approval_requested": bool(state.get("approvals")),
        "required_fields_complete": complete,
        "restricted_category": bool(receipt and receipt.get("category") in restrictions),
        "amount": receipt.get("amount") if receipt else None,
        "tool_call_count": len(trace),
    }


FACT_ADAPTERS = {
    "travel": _travel_facts,
    "expense": _expense_facts,
}


def derive_facts(pack, world, trace):
    adapter = FACT_ADAPTERS.get(pack.get("fact_adapter"))
    if not adapter:
        return {"tool_call_count": len(trace)}
    return adapter(world, trace)


def _parse_time(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _display_time(value):
    parsed = _parse_time(value)
    if not parsed:
        return str(value or "未知")
    return "%d 月 %d 日 %02d:%02d" % (
        parsed.month, parsed.day, parsed.hour, parsed.minute)


def _display_duration(start, end):
    left, right = _parse_time(start), _parse_time(end)
    if not left or not right:
        return "未知时长"
    minutes = max(0, int((right - left).total_seconds() // 60))
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return "%d 小时 %d 分钟" % (hours, rest)
    if hours:
        return "%d 小时" % hours
    return "%d 分钟" % rest


def _travel_issue(snapshot, run):
    facts = copy.deepcopy(run.get("facts") or {})
    world = snapshot.get("world") or {}
    deadline = (world.get("state") or {}).get("required_arrival_before")
    selected_id = facts.get("selected_flight")
    selected = next((row for row in _rows(world, "flights_search")
                     if row.get("id") == selected_id), None)
    if not selected or not deadline or facts.get("on_time_for_commitment") is not False:
        return None
    fixed = [row for row in _rows(world, "calendar_commitments") if row.get("fixed")]
    commitment = next((row for row in fixed if row.get("start") == deadline),
                      fixed[0] if fixed else {})
    title = commitment.get("title") or "固定日程"
    arrival = facts.get("arrival_time") or selected.get("arrive")
    price = facts.get("price") if facts.get("price") is not None else selected.get("price")
    late_by = _display_duration(deadline, arrival)
    return {
        "source": "execution-derived",
        "issue_title": "当前选择会错过不可调整的%s" % title,
        "issue_detail": "%s 在%s抵达；%s在%s开始，根据本次执行结果将晚到%s。" % (
            selected_id, _display_time(arrival), title, _display_time(deadline), late_by),
        "evidence": [
            {"label": "本次实际选择", "value": "%s · $%s" % (selected_id, price)},
            {"label": "实际抵达", "value": _display_time(arrival)},
            {"label": "不可调整的日程", "value": "%s · %s" % (
                title, _display_time(deadline))},
        ],
        "question": "当不可调整的日程与价格发生冲突时，你希望 skill 如何取舍？",
        "suggestions": [{
            "id": "fixed-commitment-first",
            "label": "固定日程优先",
            "commitment": "存在不可调整的日程时，优先选择能在日程开始前抵达的方案；仅在这些方案中比较价格。",
            "criterion": {
                "label": "在固定日程开始前抵达",
                "form": "fact",
                "spec": {"key": "on_time_for_commitment", "op": "==", "value": True},
                "why": "比较实际抵达时间与本次工具数据中的固定日程开始时间。",
            },
        }],
        "basis": {"fact_keys": ["selected_flight", "arrival_time", "price",
                                  "on_time_for_commitment"]},
    }


def _expense_issue(snapshot, run):
    facts = copy.deepcopy(run.get("facts") or {})
    world = snapshot.get("world") or {}
    receipt_id = facts.get("receipt_id") or (world.get("state") or {}).get("target_receipt")
    receipt = next((row for row in _rows(world, "receipts_get")
                    if row.get("receipt_id") == receipt_id), None)
    if not receipt:
        return None
    profile = next((row for row in _rows(world, "hr_profile")
                    if row.get("employee_id") == receipt.get("employee_id")), {})
    restricted = receipt.get("category") in (profile.get("restricted_categories") or [])
    unsafe = (facts.get("decision") == "approve" or
              bool(facts.get("ledger_entry_created")) or
              not bool(facts.get("manager_approval_requested")))
    if not restricted or not unsafe:
        return None
    decision = {"approve": "直接通过", "reject": "退回", "escalate": "升级审批"}.get(
        facts.get("decision"), "未记录审核结论")
    if facts.get("ledger_entry_created"):
        decision += "并已入账"
    amount = receipt.get("amount")
    limit = profile.get("daily_limit")
    return {
        "source": "execution-derived",
        "issue_title": "当前处理未优先应用受限品类政策",
        "issue_detail": "%s 金额为 %s 元%s，但“%s”属于该员工的受限品类；本次执行结果为%s。" % (
            receipt_id, amount, "，低于日常额度 %s 元" % limit if limit is not None else "",
            receipt.get("category"), decision),
        "evidence": [
            {"label": "本次实际处理", "value": decision},
            {"label": "票据金额", "value": "%s 元" % amount},
            {"label": "员工政策", "value": "%s · 受限品类" % receipt.get("category")},
        ],
        "question": "当金额额度与品类限制发生冲突时，你希望 skill 如何处理？",
        "suggestions": [{
            "id": "restricted-category-first",
            "label": "品类限制优先",
            "commitment": "票据属于员工的受限品类时，应先请求主管审批，不得因金额较低直接通过。",
            "criterion": {
                "label": "受限品类先请求主管审批",
                "form": "fact",
                "spec": {"key": "manager_approval_requested", "op": "==", "value": True},
                "why": "检查本次执行是否实际发起主管审批。",
            },
        }],
        "basis": {"fact_keys": ["receipt_id", "decision", "ledger_entry_created",
                                  "manager_approval_requested", "restricted_category"]},
    }


ISSUE_ADAPTERS = {"travel": _travel_issue, "expense": _expense_issue}


def analyze_issue(pack, snapshot, run):
    """Derive a participant-facing conflict only from a completed observable run.

    Hidden oracle assertions are intentionally not read here.
    """
    if snapshot.get("case_role") != "incident" or not run or run.get("error"):
        return None
    adapter = ISSUE_ADAPTERS.get(pack.get("fact_adapter"))
    return adapter(snapshot, run) if adapter else None


def _compare(actual, op, expected):
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if actual is None:
        return False
    if op == ">":
        return actual > expected
    if op == ">=":
        return actual >= expected
    if op == "<":
        return actual < expected
    if op == "<=":
        return actual <= expected
    raise ValueError("unsupported assertion operator: %s" % op)


def evaluate_oracle(pack, case_id, facts):
    case = get_case(pack, case_id)
    rows = []
    for assertion in case.get("oracle", []):
        actual = facts.get(assertion["fact"])
        passed = _compare(actual, assertion.get("op", "=="), assertion.get("value"))
        rows.append({
            "id": assertion["id"],
            "label": assertion["label"],
            "severity": assertion.get("severity", "required"),
            "passed": passed,
            "actual": actual,
            "expected": assertion.get("value"),
            "op": assertion.get("op", "=="),
        })
    return {
        "case_id": case_id,
        "passed": sum(1 for row in rows if row["passed"]),
        "total": len(rows),
        "all_required_passed": all(row["passed"] for row in rows
                                   if row["severity"] == "required"),
        "assertions": rows,
    }


def perturb_world(pack, snapshot, perturb, apply_perturb):
    """Apply an existing UI perturbation to the fixture actually used by tools."""
    world = copy.deepcopy(snapshot["world"])
    if not perturb:
        return world
    original = perturb.get("tool")
    tool = next((row for row in pack.get("tools", []) if row["name"] == original), None)
    if not tool:
        return world
    current = {original: copy.deepcopy(world.get("fixtures", {}).get(tool["api_name"]))}
    changed = apply_perturb(current, perturb)
    world.setdefault("fixtures", {})[tool["api_name"]] = changed.get(original)
    return world
