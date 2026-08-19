#!/usr/bin/env python3
"""
SkillScope 服务端。

没有预设场景：skill 由用户粘贴，世界由工具层生成后冻结，情况集从真实使用中累积。

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
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = os.environ.get("SKILLSCOPE_MODEL", "deepseek-v4-flash")
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
HERE = Path(__file__).parent
PORT = int(os.environ.get("PORT", "8000"))

LOCK = threading.Lock()
STATE = {"skills": {}, "active": None, "snapshots": {},
         "runs": [], "situations": [], "probes": [], "reviews": [], "seq": 0}


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


def shash(o):
    return hashlib.sha1(json.dumps(o, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]


# ---------------------------------------------------------------- model

def _once(system, user, max_tokens):
    body = json.dumps({"model": MODEL, "temperature": 1.0,
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
          "Do not answer the task. Output ONLY JSON: {\"args\":{}}")

P_CONNECT = ("You are an external service responding to one query. You do not know why it was asked. "
             "Return realistic, concrete results for exactly these parameters, with the ordinary "
             "variety a real service would return. Output ONLY JSON: {\"result\":<value>}")

P_EXEC = ("You simulate an agent executing the numbered skill exactly as written. Apply each "
          "instruction literally and in order. Do not introduce criteria the instructions do not "
          "state. If an instruction requires asking the user, record that as a gate step and do not "
          "assume a reply. Side-effecting tools run in dry-run: report the action without claiming "
          "external effect. Reply in the skill's language for title/detail/outcome. Output ONLY "
          'JSON: {"steps":[{"kind":"tool|filter|decision|gate|action","title":"<=10 chars",'
          '"detail":"<=24 chars","from":<instruction number>}],"facts":{},"outcome":"one sentence"}')

P_FACTS = ("List the field names that capture the decision-relevant outcome of this kind of task, "
           "so repeated executions can be compared field by field. Prefer 5-8 short snake_case keys "
           "covering what was selected, its key attributes, and whether any confirmation or "
           'approval was requested. Output ONLY JSON: {"keys":["..."]}')

P_INTENT = ("Route one message from a user of a skill-review tool. Intents: run (execute the skill "
            "on a new task), object (the last result was wrong), expect (state the behaviour they "
            "want), ask (a question about why something happened), probe (test whether an "
            "instruction matters), edit (change the instructions), publish (release the draft), "
            'other. Output ONLY JSON: {"intent":"","task":"<full task text if run>",'
            '"text":"<the substance>","instruction":<instruction number or null>}')

P_CRIT = ("Turn a user's stated expectation into checkable criteria over recorded executions. You "
          "are given the expectation, the observable fact keys the executions report, and the step "
          "kinds that appear. Propose 2-3 candidates of differing rigour. Forms: "
          'trace={"form":"trace","spec":{"must_exist":"<step kind>","before":"<step kind or null>"}};'
          ' fact={"form":"fact","spec":{"key":"<fact key>","op":"==|!=|<|>|<=|>=","value":<literal>}};'
          ' semantic={"form":"semantic","spec":{"question":"a yes/no question about one execution"}}.'
          " Prefer trace and fact over semantic. Label in the user's language. Output ONLY JSON: "
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
           "Never remove a confirmation or safety requirement unless an expectation with disposition "
           "change explicitly asks for it. Keep the original "
           'language and numbering. Output ONLY JSON: {"instructions":[{"n":1,"text":""}],'
           '"rationale":[{"n":1,"why":"one line"}]}')

P_CONTRAST = ("A workflow owner has just said what they want changed about an agent's behaviour. "
              "Propose 2 nearby situations that the same repair could accidentally affect: one where "
              "an existing behaviour should most likely be kept as it is, and one where the right "
              "policy is genuinely unclear and the owner may not want any rule decided for them. "
              "Each must be a concrete situation for the same skill, described in one short sentence "
              "in the owner's language. Output ONLY JSON: "
              '{"situations":[{"text":"","suggest":"preserve|unresolved","why":"one line"}]}')

P_JUDGE = ("Answer a yes/no question about one recorded execution. Use only the recorded steps, "
           'facts and outcome. Output ONLY JSON: {"pass":true|false,"why":"one line"}')


# ---------------------------------------------------------------- core

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


def exec_once(instructions, snap, pert=None):
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


def eval_criterion(crit, run):
    if not crit or not run or "error" in run:
        return None
    form, spec = crit.get("form"), crit.get("spec") or {}
    if form == "trace":
        kinds = [s.get("kind") for s in run.get("steps", [])]
        need = spec.get("must_exist")
        if need not in kinds:
            return False
        before = spec.get("before")
        if before:
            return True if before not in kinds else kinds.index(need) < kinds.index(before)
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


def summarize(runs, crit=None, primary=None):
    """把 k 次执行归成若干「行为组」，并指出组间差异在哪些字段。"""
    groups, ok, tot, err = {}, 0, 0, 0
    for r in runs:
        if not r or "error" in r:
            err += 1
            continue
        sig = fact_signature(r, primary) or (r.get("outcome") or "")[:60]
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

    good = len(runs) - err
    return {"groups": gl, "diff_keys": diff_keys, "fork": fork,
            "n": len(runs), "good": good, "err": err,
            "pass": ok, "tot": tot,
            "top": gl[0]["sig"] if gl else None,
            "share": round(gl[0]["n"] / float(good), 2) if gl and good else 0,
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
            return {"code": "prior", "text": "指令有效，但与模型固有倾向同向 —— 删除测不出，反转才显形"}
        if not d["changed"] and not i["changed"]:
            return {"code": "dead", "text": "指令未被遵循 —— 删除与反转都没有引起变化"}
        if d["changed"] and i["changed"]:
            return {"code": "control", "text": "指令在控制该行为"}
        return {"code": "noisy", "text": "两次探测结论不一致，需要提高重复次数"}
    if d:
        return {"code": "partial", "text": "删除后%s。再做一次反转探测，可区分「无影响」与「与模型倾向同向」"
                % ("行为改变" if d["changed"] else "无行为变化")}
    if i:
        return {"code": "partial", "text": "反转后%s" % ("行为改变" if i["changed"] else "无行为变化")}
    return None



# ---------------------------------------------------------------- 条件 B：内部操作
# 与条件 A 共用同一批核心函数。差别只在交互层：这里没有持久面板，结果以叙述返回。

P_ROUTE = ("Route one message from someone reviewing an agent skill in a chat-only tool. "
           "Pick exactly one capability, or none. Capabilities: "
           "run_task(task) execute the skill on a task; "
           "show_options() describe the candidate set of the last run and which was chosen; "
           "check_instruction(n) test whether instruction n affects behaviour; "
           "check_data() test whether the supplied data entered the decision; "
           "record_expectation(text, disposition=change|preserve|unresolved) record what the owner wants; "
           "draft() have the model revise the instructions; "
           "compare() run before and after on every recorded expectation; "
           "decide(action=publish|revise|gather|defer, reason); "
           "list_instructions(); none. "
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


def op_run(task, k=3):
    sk = cur()
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
        qa = ask(P_ARGS, "TOOL: %s%s -> %s\n\nUSER DATA:\n%s\n\nTASK: %s" % (
            name, src.get("signature") or "", src.get("returns") or "",
            json.dumps(owned, ensure_ascii=False)[:2500], task), 900)
        a_ = (qa or {}).get("args") or {}
        args[name] = a_
        res = ask(P_CONNECT, "SERVICE: %s%s -> %s\n\nPARAMETERS: %s" % (
            name, src.get("signature") or "", src.get("returns") or "",
            json.dumps(a_, ensure_ascii=False)), 3000)
        if res and "result" in res:
            tools[name] = res["result"]
        else:
            missing.append(name)
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
    acc = []
    with concurrent.futures.ThreadPoolExecutor(k) as ex:
        for r in ex.map(lambda _: exec_once(ins, snap), range(k)):
            r = r or {"error": "空返回"}
            r.update({"id": nid("r"), "sid": snap["id"], "variant": {},
                      "skill": STATE["active"]})
            acc.append(r)
    with LOCK:
        STATE["runs"].extend(acc)
    su = summarize(acc, None, (snap.get("fact_schema") or [])[:1])
    ok = [r for r in acc if "error" not in r]
    return {"snapshot": snap["id"], "summary": su,
            "workflow": ok[0].get("steps") if ok else [],
            "outcome": ok[0].get("outcome") if ok else "",
            "missing": missing}


def _last_snap():
    xs = [x for x in STATE["snapshots"].values() if x.get("skill") == STATE["active"]]
    return xs[-1] if xs else None


def op_probe(kind, n=None, k=3):
    snap = _last_snap()
    sk = cur()
    if not snap:
        return {"error": "还没有执行过任务"}
    primary = (snap.get("fact_schema") or [])[:1]
    base = [r for r in mine(STATE["runs"]) if r.get("sid") == snap["id"]
            and not r.get("variant") and "error" not in r]
    bm, bs = modal(fact_signature(r, primary) for r in base)
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
        acc = []
        with concurrent.futures.ThreadPoolExecutor(k) as ex:
            for r in ex.map(lambda _: exec_once(ins, snap, variant.get("perturb")), range(k)):
                acc.append(r or {"error": "空返回"})
        m, sh = modal(fact_signature(r, primary) for r in acc)
        out[kd] = {"changed": (bm != m) if (bm and m) else None, "share": sh}
        with LOCK:
            STATE["probes"].append({"id": nid("p"), "skill": STATE["active"], "kind": kd, "n": n,
                                    "note": "", "sid": snap["id"],
                                    "changed": out[kd]["changed"], "confidence":
                                    "ok" if bs >= .6 else "unstable",
                                    "base_share": round(bs, 2), "probe_share": round(sh or 0, 2),
                                    "k": len(acc)})
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


def op_expect(text, disposition):
    snap = _last_snap()
    if not snap:
        return {"error": "还没有执行过任务"}
    sk = cur()
    if sk.get("candidate"):
        return {"error": "已存在草稿，判定标准在本轮评审中锁定"}
    runs = [r for r in mine(STATE["runs"]) if r.get("sid") == snap["id"]
            and "error" not in r and not r.get("variant")]
    keys = sorted({k for r in runs for k in (r.get("facts") or {})})
    kinds = sorted({s.get("kind") for r in runs for s in r.get("steps", []) if s.get("kind")})
    crit = None
    if disposition == "change":
        c = ask(P_CRIT, "EXPECTATION: %s\n\nFACT KEYS: %s\n\nSTEP KINDS: %s\n\nSAMPLE:\n%s" % (
            text, keys, kinds,
            json.dumps(runs[-1], ensure_ascii=False)[:1500] if runs else "{}"), 2500)
        cands = (c or {}).get("candidates") or []
        for x in cands:
            x["trial"] = [eval_criterion(x, r) for r in runs[-3:]]
        good = [x for x in cands if x["trial"] and any(v is not None for v in x["trial"])]
        crit = (good or cands or [None])[0]
    st = {"id": nid("t"), "skill": STATE["active"], "sid": snap["id"], "commitment": text,
          "criterion": crit, "disposition": disposition, "label": (crit or {}).get("label", ""),
          "sealed": False, "created": time.time()}
    with LOCK:
        sk["scope_version"] = (sk.get("scope_version") or 1) + 1
        st["scope_version"] = sk["scope_version"]
        STATE["situations"].append(st)
    return {"recorded": text, "disposition": disposition,
            "criterion": (crit or {}).get("label"), "scope_version": sk["scope_version"]}


def op_compare(k=3):
    sits = mine(STATE["situations"])
    if not sits:
        return {"error": "还没有记录任何预期"}
    rows = []
    for x in sits:
        snap = STATE["snapshots"].get(x.get("sid"))
        if not snap:
            continue
        primary = (snap.get("fact_schema") or [])[:1]
        res = {}
        for w in ("base", "draft"):
            variant = {"draft": True} if w == "draft" else {}
            ins, _ = instructions_for(variant)
            acc = []
            with concurrent.futures.ThreadPoolExecutor(k) as ex:
                for r in ex.map(lambda _: exec_once(ins, snap), range(k)):
                    r = r or {"error": "空返回"}
                    r["_pass"] = eval_criterion(x.get("criterion"), r)
                    acc.append(r)
            su = summarize(acc, x.get("criterion"), primary)
            res[w] = su
        b, a = res["base"], res["draft"]
        moved = (b["top"] != a["top"])
        if b["share"] < .6 or a["share"] < .6:
            verdict = "insufficient"
        elif x["disposition"] == "change":
            verdict = "met" if (a["pass"] > a["tot"] / 2 if a["tot"] else moved) else "unmet"
        elif x["disposition"] == "preserve":
            broken = (b["pass"] > 0 and a["pass"] == 0) if (b["tot"] and a["tot"]) else moved
            verdict = "broken" if broken else "kept"
        else:
            verdict = "decided" if moved else "untouched"
        rows.append({"expectation": x["commitment"], "disposition": x["disposition"],
                     "verdict": verdict})
    return {"rows": rows}

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
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

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
        path = self.path.split("?")[0]
        if path in ("/chat", "/chat.html"):
            f = HERE / "chat.html"
            return self._send(200, "text/html; charset=utf-8", f.read_bytes()) if f.exists() \
                else self._send(404, "text/plain", b"chat.html missing")
        if path in ("/", "/index.html", "/app.html"):
            f = HERE / "app.html"
            return self._send(200, "text/html; charset=utf-8", f.read_bytes()) if f.exists() \
                else self._send(404, "text/plain", b"app.html missing")
        if path == "/api/state":
            return self._json({
                "skills": [{"id": k, "name": v["name"], "version": v["version"],
                            "hash": v["hash"], "n": len(v["instructions"]),
                            "draft": bool(v.get("candidate"))}
                           for k, v in STATE["skills"].items()],
                "active": STATE["active"],
                "skill": cur(), "candidate": (cur() or {}).get("candidate"),
                "scope_version": (cur() or {}).get("scope_version") or 1,
                "sources": [{"tool": k, "label": v.get("label") or k, "kind": v.get("kind"),
                             "signature": v.get("signature"), "returns": v.get("returns"),
                             "rows": v.get("rows") or [], "n": len(v.get("rows") or [])}
                            for k, v in ((cur() or {}).get("sources") or {}).items()],
                "versions": len((cur() or {}).get("versions") or []),
                "snapshots": [x for x in STATE["snapshots"].values()
                              if x.get("skill") == STATE["active"]],
                "situations": mine(STATE["situations"]), "probes": mine(STATE["probes"]),
                "reviews": mine(STATE.get("reviews") or []),
                "history": [{"version": v.get("version"), "hash": v.get("hash"),
                             "n": len(v.get("instructions") or [])}
                            for v in ((cur() or {}).get("versions") or [])],
                "model": MODEL, "keyed": bool(API_KEY)})
        return self._send(404, "text/plain", b"not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        if not API_KEY:
            return self._json({"error": "DEEPSEEK_API_KEY 未设置"}, 500)
        fn = getattr(self, "h_" + path.strip("/").replace("/", "_"), None)
        if fn is None:
            return self._send(404, "text/plain", b"not found")
        try:
            return fn()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("!! %s: %s\n" % (type(e).__name__, e))
            try:
                return self._json({"error": "%s: %s" % (type(e).__name__, str(e)[:200])}, 500)
            except Exception:  # noqa: BLE001
                return

    # -------- endpoints

    def h_api_skill(self):
        b = self._body()
        out = ask(P_PARSE, b.get("text", "")[:12000], 4000)
        if not out or "instructions" not in out:
            return self._json(out or {"error": "解析失败"}, 500)
        sid = nid("k")
        sk = {"id": sid, "name": out.get("name") or "未命名",
              "instructions": out["instructions"], "tools": out.get("tools") or [],
              "config": out.get("config") or {}, "version": 1,
              "versions": [], "candidate": None, "scope_version": 1,
              "sources": {t["name"]: {"label": t.get("label") or t["name"],
                                      "kind": t.get("kind") or
                                      ("write" if t.get("side_effecting") else "query"),
                                      "signature": t.get("signature") or "",
                                      "returns": t.get("returns") or "", "rows": []}
                          for t in (out.get("tools") or [])}}
        sk["hash"] = shash(sk["instructions"])
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
            if STATE["active"] == i:
                STATE["active"] = next(iter(STATE["skills"]), None)
        return self._json({"ok": True, "active": STATE["active"]})

    def h_api_export(self):
        """评审记录：skill 版本、快照、执行、情况与判据、探测与结论。"""
        a = STATE["active"]
        sk = cur()
        if not sk:
            return self._json({"error": "无 skill"}, 400)
        return self._json({
            "format": "skillscope/1", "exported": time.time(), "model": MODEL,
            "skill": sk,
            "snapshots": [x for x in STATE["snapshots"].values() if x.get("skill") == a],
            "runs": mine(STATE["runs"]), "situations": mine(STATE["situations"]),
            "probes": mine(STATE["probes"]), "reviews": mine(STATE.get("reviews") or [])})

    def h_api_import(self):
        b = self._body()
        sk = b.get("skill")
        if not sk or "instructions" not in sk:
            return self._json({"error": "包格式不正确"}, 400)
        sid = nid("k")
        sk = dict(sk)
        sk["id"] = sid
        sk.setdefault("versions", [])
        sk.setdefault("candidate", None)
        with LOCK:
            STATE["skills"][sid] = sk
            STATE["active"] = sid
            for snap in b.get("snapshots") or []:
                snap = dict(snap); snap["skill"] = sid
                STATE["snapshots"][snap["id"]] = snap
            STATE.setdefault("reviews", [])
            for key, dst in (("runs", STATE["runs"]), ("situations", STATE["situations"]),
                             ("probes", STATE["probes"]), ("reviews", STATE["reviews"])):
                for x in b.get(key) or []:
                    x = dict(x); x["skill"] = sid; dst.append(x)
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
        srcs = sk.get("sources") or {}
        tools, args, missing = {}, {}, []

        ordered = sorted(srcs.items(),
                         key=lambda kv: {"data": 0, "query": 1, "write": 2}.get(kv[1].get("kind"), 1))
        for name, src in ordered:
            kind = src.get("kind")
            if kind == "write":
                continue                       # 写入型不预取，评审期一律 dry-run
            if kind == "data":
                if src.get("rows"):
                    tools[name] = src["rows"]
                else:
                    missing.append(name)
                continue
            # query：先只从任务里取出调用参数，再把参数交给连接器
            owned = {k: v for k, v in tools.items()
                     if (srcs.get(k) or {}).get("kind") == "data"}
            qa = ask(P_ARGS, "TOOL: %s%s -> %s\n\nUSER DATA:\n%s\n\nTASK: %s" % (
                name, src.get("signature") or "", src.get("returns") or "",
                json.dumps(owned, ensure_ascii=False)[:2500], task), 900)
            a_ = (qa or {}).get("args") or {}
            args[name] = a_
            res = ask(P_CONNECT, "SERVICE: %s%s -> %s\n\nPARAMETERS: %s" % (
                name, src.get("signature") or "", src.get("returns") or "",
                json.dumps(a_, ensure_ascii=False)), 3000)
            if res and "result" in res:
                tools[name] = res["result"]
            else:
                missing.append(name)

        if not tools:
            return self._json({"error": "没有可用的数据源", "missing": missing}, 400)

        snap = {"id": nid("s"), "task": task, "tools": tools, "args": args,
                "missing": missing, "summary": "", "fact_schema": None,
                "skill": STATE["active"], "recorded": time.time()}
        with LOCK:
            STATE["snapshots"][snap["id"]] = snap
        return self._json({"snapshot": snap})

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
        with concurrent.futures.ThreadPoolExecutor(k) as ex:
            futs = [ex.submit(exec_once, instructions, snap, variant.get("perturb"))
                    for _ in range(k)]
            for fut in concurrent.futures.as_completed(futs):
                r = fut.result() or {"error": "空返回"}
                r["_pass"] = eval_criterion(crit, r) if crit else None
                r.update({"id": nid("r"), "sid": snap["id"], "variant": variant,
                          "skill": STATE["active"]})
                acc.append(r)
                with LOCK:
                    STATE["runs"].append(r)
                if not quiet:
                    self._chunk({"type": "run", "run": r})
                if on_run:
                    on_run(r)
        return acc

    def h_api_run(self):
        b = self._body()
        snap = STATE["snapshots"].get(b.get("snapshot"))
        if not snap or not cur():
            return self._json({"error": "快照不存在"}, 400)
        variant = b.get("variant") or {}
        k = max(1, min(8, int(b.get("k", 3))))
        crit = b.get("criterion")
        if not snap.get("fact_schema"):
            fs = ask(P_FACTS, "TASK: %s\nTOOLS: %s" % (snap["task"], ", ".join(snap["tools"])), 900)
            if fs and "keys" in fs:
                snap["fact_schema"] = fs["keys"]
        instructions, base = instructions_for(variant)
        self._open()
        acc = self._stream_runs(instructions, snap, variant, k, crit)
        self._chunk({"type": "done", "summary": summarize(acc, crit,
                                                          (snap.get("fact_schema") or [])[:1]),
                     "skill_hash": base["hash"], "snapshot": snap["id"]})
        self._close()

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
        lines = "\n".join("%d. %s" % (i["n"], i["text"]) for i in sk["instructions"])
        have = [x["commitment"] for x in mine(STATE["situations"])]
        out = ask(P_CONTRAST, "SKILL:\n%s\n\nWHAT THEY WANT CHANGED: %s\n\nALREADY RECORDED: %s"
                  % (lines, b.get("commitment", ""), json.dumps(have, ensure_ascii=False)), 1500)
        return self._json(out or {"situations": []})

    def h_api_situation(self):
        b = self._body()
        sk0 = cur()
        if sk0 and sk0.get("candidate"):
            return self._json({"error": "已存在草稿，判定标准在本轮评审中锁定"}, 409)
        st = {"id": nid("t"), "skill": STATE["active"], "sid": b.get("snapshot"),
              "commitment": b.get("commitment", ""),
              "criterion": b.get("criterion"), "disposition": b.get("disposition", "mod"),
              "label": b.get("label", ""), "sealed": bool(b.get("sealed")), "created": time.time()}
        with LOCK:
            sk = cur()
            if sk:
                sk["scope_version"] = (sk.get("scope_version") or 1) + 1
                st["scope_version"] = sk["scope_version"]
            STATE["situations"].append(st)
        return self._json({"situation": st, "scope_version": (sk or {}).get("scope_version")})

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
                     and not r.get("variant") and "error" not in r]
        primary = (snap.get("fact_schema") or [])[:1]
        base_mode, base_share = modal(fact_signature(r, primary) for r in base_runs)

        k = max(1, min(6, int(b.get("k", 4))))
        instructions, _ = instructions_for(variant)
        self._open()
        self._chunk({"type": "probe", "kind": kind, "n": n, "note": note, "variant": variant,
                     "baseline": {"share": base_share, "n": len(base_runs)}})
        acc = self._stream_runs(instructions, snap, variant, k, None)
        probe_mode, probe_share = modal(fact_signature(r, primary) for r in acc)

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
                     and not r.get("variant") and "error" not in r]
        primary = (snap.get("fact_schema") or [])[:1]
        base_mode, base_share = modal(fact_signature(r, primary) for r in base_runs)
        k = max(1, min(5, int(b.get("k", 3))))

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
            mode, share = modal(fact_signature(r, primary) for r in acc)
            rec = {"id": nid("p"), "skill": STATE["active"], "kind": kind, "n": n,
                   "note": note or ("移除指令 %d" % n), "sid": snap["id"],
                   "changed": (base_mode != mode) if (base_mode and mode) else None,
                   "confidence": "unstable" if (base_share < .6 or share < .6) else "ok",
                   "base_share": round(base_share, 2), "probe_share": round(share, 2),
                   "k": len(acc)}
            with LOCK:
                STATE["probes"].append(rec)
            results[kind] = rec

        d, i = results.get("delete"), results.get("invert")
        # 只有基线自身波动才让结论失效；探测结果本身的波动不影响「变没变」
        if base_share < 0.6 or not d or not i or d["changed"] is None or i["changed"] is None:
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
        """条件 B：纯对话。能力与条件 A 完全相同，仅在被要求时调用，结果以叙述返回。"""
        b = self._body()
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        msg = (b.get("message") or "").strip()
        hist = b.get("history") or []
        STATE.setdefault("chat", [])

        ctx = "SKILL: %s\nINSTRUCTIONS:\n%s\nRECORDED EXPECTATIONS: %d\nDRAFT: %s\nLAST RUN: %s" % (
            sk["name"],
            "\n".join("%d. %s" % (i["n"], i["text"]) for i in sk["instructions"]),
            len(mine(STATE["situations"])), "yes" if sk.get("candidate") else "no",
            "yes" if _last_snap() else "no")
        convo = "\n".join("%s: %s" % (h.get("role"), h.get("text", "")[:400]) for h in hist[-8:])
        r = ask(P_ROUTE, "%s\n\nCONVERSATION:\n%s\n\nMESSAGE: %s" % (ctx, convo, msg), 900)
        cap = (r or {}).get("capability") or "none"
        args = (r or {}).get("args") or {}

        self._open()
        self._chunk({"type": "act", "capability": cap})

        # 起草前询问一次必须保持不变的行为；这是能力，不是界面
        if cap == "draft" and not any(x["disposition"] == "preserve"
                                      for x in mine(STATE["situations"])):
            q = ask(P_ASKPRESERVE, "EXPECTATIONS SO FAR: %s" % json.dumps(
                [x["commitment"] for x in mine(STATE["situations"])], ensure_ascii=False), 700)
            self._chunk({"type": "text", "text": (q or {}).get("reply") or
                         "在我修改之前，有哪些现有行为必须保持不变？"})
            self._close()
            return

        try:
            if cap == "run_task":
                res = op_run(args.get("task") or msg)
            elif cap == "show_options":
                snap = _last_snap()
                if not snap:
                    res = {"error": "还没有执行过任务"}
                else:
                    srcs = sk.get("sources") or {}
                    opts, chosen = None, None
                    for tname, val in snap["tools"].items():
                        if (srcs.get(tname) or {}).get("kind") == "query" \
                           and isinstance(val, list) and val:
                            opts = val
                            break
                    runs = [r for r in mine(STATE["runs"]) if r.get("sid") == snap["id"]
                            and not r.get("variant") and "error" not in r]
                    if runs:
                        chosen = runs[-1].get("facts")
                    res = {"options": opts, "chosen_facts": chosen} if opts \
                        else {"error": "本次没有候选集合"}
            elif cap == "check_instruction":
                res = op_probe("instruction", int(args.get("n") or 0))
            elif cap == "check_data":
                res = op_probe("data")
            elif cap == "record_expectation":
                res = op_expect(args.get("text") or msg, args.get("disposition") or "change")
            elif cap == "draft":
                res = self._draft_internal()
            elif cap == "compare":
                res = op_compare()
            elif cap == "decide":
                res = self._decide_internal(args.get("action"), args.get("reason") or "")
            elif cap == "list_instructions":
                res = {"instructions": sk["instructions"]}
            else:
                res = {"note": "未调用任何能力"}
        except Exception as e:  # noqa: BLE001
            res = {"error": "%s: %s" % (type(e).__name__, str(e)[:160])}

        nar = ask(P_NARRATE, "CAPABILITY: %s\n\nRESULT:\n%s\n\nUSER MESSAGE: %s" % (
            cap, json.dumps(res, ensure_ascii=False)[:4000], msg), 1200)
        self._chunk({"type": "text", "text": (nar or {}).get("reply") or
                     json.dumps(res, ensure_ascii=False)[:400]})
        self._close()

    def _draft_internal(self):
        sk = cur()
        exps = [{"expectation": x["commitment"], "disposition": x["disposition"]}
                for x in mine(STATE["situations"]) if not x.get("sealed")]
        ev = [{"instruction": p["n"], "probe": p["kind"], "changed": p["changed"]}
              for p in mine(STATE["probes"]) if p.get("n")]
        lines = "\n".join("%d. %s" % (i["n"], i["text"]) for i in sk["instructions"])
        out = ask(P_DRAFT, "SKILL:\n%s\n\nEXPECTATIONS:\n%s\n\nPROBE EVIDENCE:\n%s" % (
            lines, json.dumps(exps, ensure_ascii=False), json.dumps(ev, ensure_ascii=False)), 4000)
        if not out or "instructions" not in out:
            return {"error": "起草失败"}
        cand = {"id": sk["id"], "name": sk["name"], "instructions": out["instructions"],
                "tools": sk["tools"], "config": sk["config"], "version": sk["version"] + 1,
                "versions": [], "candidate": None, "rationale": out.get("rationale", [])}
        cand["hash"] = shash(cand["instructions"])
        cand["scope_version"] = sk.get("scope_version") or 1
        with LOCK:
            sk["candidate"] = cand
        return {"drafted": True, "instructions": cand["instructions"]}

    def _decide_internal(self, action, reason):
        sk = cur()
        if action == "publish":
            if not sk.get("candidate"):
                return {"error": "没有草稿"}
            with LOCK:
                cand = sk["candidate"]
                hist = list(sk.get("versions") or [])
                hist.append({k: sk[k] for k in ("name", "instructions", "version", "hash")})
                cand["versions"] = hist
                cand["candidate"] = None
                STATE["skills"][sk["id"]] = cand
        rec = {"id": nid("v"), "skill": STATE["active"], "action": action, "reason": reason,
               "at": time.time(), "skill_hash": sk["hash"],
               "scope_version": sk.get("scope_version") or 1,
               "condition": "chat",
               "situations": [{"commitment": x["commitment"], "disposition": x["disposition"]}
                              for x in mine(STATE["situations"])]}
        with LOCK:
            STATE.setdefault("reviews", []).append(rec)
        return {"decided": action, "reason": reason}

    def h_api_manifest(self):
        """起草前交给用户确认：哪些内容会进入起草模型，哪些不会。"""
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        sits = mine(STATE["situations"])
        probes = [p for p in mine(STATE["probes"]) if p.get("n")]
        return self._json({
            "scope_version": sk.get("scope_version") or 1,
            "skill_hash": sk["hash"],
            "visible": {
                "expectations": [{"text": x["commitment"], "disposition": x["disposition"]}
                                 for x in sits if not x.get("sealed")],
                "probes": len(probes),
                "instructions": len(sk["instructions"])},
            "withheld": {
                "expectations": [{"text": x["commitment"], "disposition": x["disposition"]}
                                 for x in sits if x.get("sealed")]}})

    def h_api_draft(self):
        sk = cur()
        if not sk:
            return self._json({"error": "尚未导入 skill"}, 400)
        b = self._body()
        exps = [{"expectation": s["commitment"], "disposition": s["disposition"],
                  "task": (STATE["snapshots"].get(s.get("sid")) or {}).get("task", "")}
                for s in mine(STATE["situations"]) if not s.get("sealed")]
        fb = b.get("feedback") or []
        ev = [{"instruction": p["n"], "probe": p["kind"], "changed": p["changed"], "note": p["note"]}
              for p in mine(STATE["probes"]) if p.get("n")]
        lines = "\n".join("%d. %s" % (i["n"], i["text"]) for i in sk["instructions"])
        extra = ("\n\nPREVIOUS ATTEMPT — what the last revision actually did. Fix these without "
                 "losing what already works:\n" + json.dumps(fb, ensure_ascii=False)) if fb else ""
        out = ask(P_DRAFT, "SKILL:\n%s\n\nEXPECTATIONS:\n%s\n\nPROBE EVIDENCE:\n%s%s" % (
            lines, json.dumps(exps, ensure_ascii=False),
            json.dumps(ev, ensure_ascii=False), extra), 4000)
        if not out or "instructions" not in out:
            return self._json(out or {"error": "起草失败"}, 500)
        cand = {"id": sk["id"], "name": sk["name"], "instructions": out["instructions"],
                "tools": sk["tools"], "config": sk["config"], "version": sk["version"] + 1,
                "versions": [], "candidate": None, "rationale": out.get("rationale", [])}
        cand["hash"] = shash(cand["instructions"])
        cand["scope_version"] = sk.get("scope_version") or 1
        with LOCK:
            sk["candidate"] = cand
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
        cand = sk.get("candidate")
        if not cand:
            cand = {"id": sk["id"], "name": sk["name"], "tools": sk["tools"],
                    "config": sk["config"], "version": sk["version"] + 1,
                    "versions": [], "candidate": None, "rationale": []}
        cand["instructions"] = [{"n": i.get("n"), "text": (i.get("text") or "").strip()}
                                for i in ins if (i.get("text") or "").strip()]
        cand["hash"] = shash(cand["instructions"])
        cand["author"] = "owner"
        cand["scope_version"] = sk.get("scope_version") or 1
        with LOCK:
            sk["candidate"] = cand
        return self._json({"candidate": cand})

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
               "situations": [{"commitment": x["commitment"], "disposition": x["disposition"],
                               "sealed": bool(x.get("sealed"))}
                              for x in mine(STATE["situations"])],
               "outcome": b.get("outcome") or []}
        with LOCK:
            STATE.setdefault("reviews", []).append(rec)
        return self._json({"review": rec})

    def h_api_blockprobe(self):
        """对候选的某条指令做临时移除，估计该改动块的影响范围。
        基线是候选本身，与对原 skill 的定位证据分属不同类型。"""
        b = self._body()
        snap = STATE["snapshots"].get(b.get("snapshot"))
        sk = cur()
        if not snap or not sk or not sk.get("candidate"):
            return self._json({"error": "需要先有草稿"}, 400)
        n = b.get("n")
        k = max(1, min(5, int(b.get("k", 3))))
        primary = (snap.get("fact_schema") or [])[:1]

        base = [r for r in mine(STATE["runs"])
                if r.get("sid") == snap["id"] and (r.get("variant") or {}).get("draft")
                and not (r.get("variant") or {}).get("mask") and "error" not in r]
        base_mode, base_share = modal(fact_signature(r, primary) for r in base)

        variant = {"draft": True, "mask": [n]}
        instructions, _ = instructions_for(variant)
        self._open()
        self._chunk({"type": "start", "n": n, "baseline": "candidate"})
        acc = self._stream_runs(instructions, snap, variant, k, None, quiet=True)
        mode, share = modal(fact_signature(r, primary) for r in acc)
        if base_mode is None or mode is None or base_share < 0.6:
            code, changed = "unsure", None
        else:
            changed = (base_mode != mode)
            code = "responsible" if changed else "elsewhere"
        rec = {"id": nid("p"), "skill": STATE["active"], "kind": "block", "n": n,
               "note": "移除候选中的指令 %d" % n, "sid": snap["id"],
               "changed": changed, "confidence": "ok" if code != "unsure" else "unstable",
               "base_share": round(base_share, 2), "probe_share": round(share, 2), "k": len(acc)}
        with LOCK:
            STATE["probes"].append(rec)
        self._chunk({"type": "done", "probe": rec, "code": code})
        self._close()

    def h_api_publish(self):
        sk = cur()
        if not sk or not sk.get("candidate"):
            return self._json({"error": "没有草稿"}, 400)
        with LOCK:
            cand = sk["candidate"]
            hist = list(sk.get("versions") or [])
            hist.append({k: sk[k] for k in ("name", "instructions", "version", "hash")})
            cand["versions"] = hist
            cand["candidate"] = None
            STATE["skills"][sk["id"]] = cand
            STATE.setdefault("reviews", []).append(
                {"id": nid("v"), "skill": sk["id"], "action": "publish",
                 "reason": "", "at": time.time(), "skill_hash": sk["hash"],
                 "candidate_hash": cand["hash"], "version": cand["version"],
                 "scope_version": sk.get("scope_version") or 1,
                 "situations": [{"commitment": x["commitment"], "disposition": x["disposition"],
                                 "sealed": bool(x.get("sealed"))}
                                for x in mine(STATE["situations"])]})
        return self._json({"skill": cur()})

    def h_api_discard(self):
        sk = cur()
        if sk:
            with LOCK:
                sk["candidate"] = None
        return self._json({"ok": True})


def main():
    if not API_KEY:
        print("!! 未设置 DEEPSEEK_API_KEY", file=sys.stderr)
    print("SkillScope  http://127.0.0.1:%d   模型 %s" % (PORT, MODEL))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
