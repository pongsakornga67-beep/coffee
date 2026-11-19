"""LLM gateway ปลอม: พูด OpenAI /v1/chat/completions เหมือน gateway ของงาน (model "event-llm")

- จำกัดเหมือนของจริง: พร้อมกัน 2 คำขอ/ทีม (TOO_MANY_CONCURRENT) · 20 คำขอ/นาที (RATE_LIMIT) · max_tokens ≤ 2000
  · context รวม 8,192 token
- ตอบ JSON ตาม response_format.json_schema: อ่าน prompt ของ starter (strategy.py) และ notebook แล้วตอบแบบ
  "LLM ตัวเล็ก": พอใช้ได้ แต่มักเสนอน้อยไป ตัวคูณแกว่ง
- จำลองความพัง: ช้า, ตอบ JSON ขาด (finish_reason=length), 500 · ปรับด้วย --llm-fail-rate / --llm-latency
- --llm-upstream http://localhost:11434/v1 = ส่งต่อไป Ollama จริงแทน (ฝึกนอกเวลาแข่ง)
"""
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request

LOCK = threading.Lock()
ACTIVE = {}     # team -> จำนวนคำขอที่กำลังทำ
CALLS = {}      # team -> [เวลาเรียก]
RNG = random.Random()


def _err(status, code, message, extra=None):
    return status, {"error": {"code": code, "message": message, "type": code.lower()}}, extra or {}


def _team(headers):
    auth = headers.get("Authorization", "")
    m = re.search(r"sk-(T\d{1,3})", auth)
    return m.group(1) if m else "T01"


def _tokens(text):
    thai = sum(1 for c in text if "฀" <= c <= "๿")
    return int((len(text) - thai) / 3.6 + thai / 1.4) + 1


def handle(method, path, raw, headers, args):
    path = path.rstrip("/")
    if method == "GET" and path in ("/v1/models", "/models"):
        return 200, {"object": "list", "data": [{"id": "event-llm", "object": "model", "owned_by": "mock"}]}, {}
    if not (method == "POST" and path in ("/v1/chat/completions", "/chat/completions")):
        return _err(404, "NOT_FOUND", f"No {method} {path} on the mock LLM gateway (use /llm/v1/chat/completions).")
    try:
        req = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return _err(400, "BAD_REQUEST", "Body is not JSON.")
    team = _team(headers)
    now = time.time()
    with LOCK:
        CALLS[team] = [t for t in CALLS.get(team, []) if now - t < 60]
        if len(CALLS[team]) >= 20:
            wait = int(60 - (now - CALLS[team][0])) + 1
            return _err(429, "RATE_LIMIT", "More than 20 requests per minute.", {"Retry-After": str(wait)})
        if ACTIVE.get(team, 0) >= 2:
            return _err(429, "TOO_MANY_CONCURRENT", "Your team already has 2 requests running.")
        CALLS[team].append(now)
        ACTIVE[team] = ACTIVE.get(team, 0) + 1
    try:
        return _complete(req, args)
    finally:
        with LOCK:
            ACTIVE[team] -= 1


def _complete(req, args):
    max_tokens = int(req.get("max_tokens") or req.get("max_completion_tokens") or 1000)
    if max_tokens > 2000:
        return _err(400, "BAD_REQUEST", "max_tokens must be at most 2000 on this gateway.")
    msgs = req.get("messages") or []
    prompt = "\n".join(str(m.get("content", "")) for m in msgs)
    p_tok = _tokens(prompt)
    if p_tok + max_tokens > 8192:
        return _err(400, "CONTEXT_LENGTH_EXCEEDED",
                    f"This model's maximum context length is 8192 tokens; you asked for {p_tok} + {max_tokens}.")
    effort = req.get("reasoning_effort")
    ctk = req.get("chat_template_kwargs") or {}
    thinking = (effort not in (None, "none", "minimal")) or bool(ctk.get("enable_thinking"))

    if args.llm_upstream:
        return _forward(req, args)

    # ความช้า: ราว 0.6 วิ + 25 ms/100 token ของ prompt · thinking ช้ากว่ามาก
    delay = (0.6 + p_tok / 4000 + RNG.uniform(0, 1.2)) * args.llm_latency
    if thinking:
        delay *= RNG.uniform(5, 12)
    roll = RNG.random()
    if roll < args.llm_fail_rate * 0.3:
        time.sleep(min(delay, 2))
        return _err(500, "UPSTREAM_ERROR", "The model server returned an error (simulated).")
    if roll < args.llm_fail_rate * 0.6:
        delay = 90 * max(args.llm_latency, 0.05)     # ช้ามากจน agent ควร timeout
    time.sleep(delay)

    schema = (((req.get("response_format") or {}).get("json_schema") or {}).get("schema"))
    user = next((m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
    temp = float(req.get("temperature") if req.get("temperature") is not None else 0.7)
    if schema:
        answer = _answer(schema, str(user), temp)
        text = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
    else:
        text = "ตลาดจำลอง: LLM ปลอมตอบข้อความทั่วไป ถ้าต้องการคำตอบที่ใช้งานได้ให้ส่ง response_format แบบ json_schema"
    finish = "stop"
    if thinking:
        text = "<think>\nLooking at the results and candidates...\n</think>\n" + text
    c_tok = _tokens(text)
    if c_tok > max_tokens or (args.llm_fail_rate * 0.6 <= roll < args.llm_fail_rate):
        text = text[: max(5, min(len(text), int(max_tokens * 3.6)) * 2 // 3)]   # JSON ขาดกลางทาง
        finish, c_tok = "length", min(c_tok, max_tokens)
    return 200, {"id": f"chatcmpl-mock{int(time.time() * 1000)}", "object": "chat.completion", "created": int(time.time()),
                 "model": req.get("model") or "event-llm",
                 "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": p_tok, "completion_tokens": c_tok, "total_tokens": p_tok + c_tok}}, {}


def _forward(req, args):
    req = dict(req)
    req["model"] = args.llm_model
    url = args.llm_upstream.rstrip("/") + "/chat/completions"
    r = urllib.request.Request(url, data=json.dumps(req).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read()), {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read()), {}
        except Exception:       # noqa: BLE001
            return _err(e.code, "UPSTREAM_ERROR", str(e))
    except Exception as e:      # noqa: BLE001
        return _err(502, "UPSTREAM_ERROR", f"Cannot reach {url}: {e}")


# ---------------------------------------------------------------- สร้างคำตอบตาม schema

TYPES = ("SPECIALTY", "COMMERCIAL", "INSTANT")
BASE = {"SPECIALTY": 1.10, "COMMERCIAL": 1.00, "INSTANT": 0.93}


def _json_after(label, text, opener):
    i = text.find(label)
    if i < 0:
        return None
    j = text.find(opener, i)
    if j < 0:
        return None
    depth, close = 0, {"[": "]", "{": "}"}[opener]
    for k in range(j, len(text)):
        if text[k] == opener:
            depth += 1
        elif text[k] == close:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _markups_from(text):
    for label in ("Suggested markups", "Current markups", "markups"):
        m = _json_after(label, text, "{")
        if isinstance(m, dict) and any(t in m for t in TYPES):
            return {t: float(m.get(t, BASE[t])) for t in TYPES}
    return dict(BASE)


def _learn(text, mk):
    """ปรับตัวคูณจากบรรทัดผลรอบในแบบ 'SPECIALTY x1.12 ABOVE_LIMIT'"""
    why = []
    for t, x, status in re.findall(r"(SPECIALTY|COMMERCIAL|INSTANT)\s+x([0-9.]+)\s+([A-Z_]+)", text):
        try:
            x = float(x)
        except ValueError:
            continue
        if status == "ABOVE_LIMIT":
            mk[t] = min(mk[t], x - 0.04)
            why.append(f"{t} too high")
        elif status == "ACCEPTED":
            mk[t] = max(mk[t], x + 0.02)
        elif status == "OUTBID":
            mk[t] = min(mk[t], x - 0.02)
    return why


def _answer(schema, user, temp):
    props = schema.get("properties", {})
    jitter = 0.04 * temp
    # starter strategy.py: {"offers": [{"candidate", "bags", "markup"}], "reason"}
    if "offers" in props:
        cands = _json_after("Candidates", user, "[") or []
        mk = _markups_from(user)
        why = _learn(user, mk)
        m = re.search(r"about (\d+) bags", user)
        target = int(m.group(1)) if m else 6
        m = re.search(r"at most (\d+) bags in total", user)
        cap = int(m.group(1)) if m else 10 ** 6
        goal = max(1, min(cap, int(target * RNG.uniform(0.55, 1.0))))   # ตัวเล็กมักเสนอน้อยไป
        lot_used, offers = {}, []
        order = sorted(cands, key=lambda c: -(BASE.get(c.get("buyer_type"), 1) + RNG.uniform(0, 0.1)))
        for c in order:
            if goal <= 0:
                break
            bags = min(int(c.get("max_bags", 1)), goal)
            if bags <= 0:
                continue
            t = c.get("buyer_type", "COMMERCIAL")
            offers.append({"candidate": int(c.get("candidate", 0)), "bags": bags,
                           "markup": round(mk.get(t, 1.0) + RNG.uniform(-jitter, jitter), 3)})
            goal -= bags
            lot_used[c.get("lot_id")] = lot_used.get(c.get("lot_id"), 0) + bags
        reason = ("Lowered " + ", ".join(sorted(set(why))) + "; " if why else "") + \
                 f"spread {sum(o['bags'] for o in offers)} bags, best-paying buyers first."
        return {"offers": offers, "reason": reason[:300]}
    # notebook ข้อ 3: {"markup": {SPECIALTY, COMMERCIAL, INSTANT}, "reason"}
    if "markup" in props and props["markup"].get("type") == "object":
        mk = _markups_from(user)
        why = _learn(user, mk)
        mk = {t: round(v + RNG.uniform(-jitter, jitter), 2) for t, v in mk.items()}
        keys = props["markup"].get("properties") or mk
        return {"markup": {t: mk.get(t, 1.0) for t in keys},
                "reason": ("Adjusted after: " + ", ".join(sorted(set(why)))) if why else "Specialty pays most, Instant least."}
    return _fill(schema)


def _fill(s):
    t = s.get("type")
    if "enum" in s:
        return s["enum"][0]
    if t == "object" or "properties" in s:
        props = s.get("properties", {})
        keys = s.get("required") or list(props)
        return {k: _fill(props.get(k, {})) for k in keys}
    if t == "array":
        return [_fill(s["items"])] if s.get("minItems") and s.get("items") else []
    if t == "number":
        return round(RNG.uniform(0.95, 1.1), 2)
    if t == "integer":
        return 1
    if t == "boolean":
        return True
    return "ok (mock LLM)"
