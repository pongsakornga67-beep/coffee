"""ตลาดเมล็ดกาแฟจำลอง (local) — API v1 แบบเดียวกับตลาดจริง + หน้าเว็บ /trade + LLM gateway ปลอม

    python server.py                    # preset fast ที่ http://localhost:9000
    python server.py --preset sandbox   # เหมือนตลาดฝึกวันที่ 18
    python server.py --preset contest   # เหมือนช่วงแข่ง 2–3 (กด Start ในหน้าเว็บ)
    python server.py --help

ใช้ Python มาตรฐานล้วน ไม่ต้อง pip install · ไม่ยุ่งกับพอร์ต 8000/8001 และ key จริง
key อะไรก็ได้ (sk-T05-xxx → ทีม T05) ไม่ใส่ key = ทีม T01
"""
import argparse
import atexit
import subprocess
import io
import json
import os
import re
import secrets
import socket
import webbrowser
import sys
import threading
import time
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import fake_llm                                     # noqa: E402
from config import PRESETS                          # noqa: E402
from engine import BAG, ApiError, Exchange, grade   # noqa: E402

HERE = Path(__file__).parent
VERSION = "mock-1.0"
EX: Exchange = None
ARGS = None
BUCKETS = {}            # team -> [tokens, last_time]   จำกัด 5 ครั้ง/วินาที กระชากได้ 10
KEYS = {}               # key อะไรก็ได้ -> team_id (ไม่ต้องลงทะเบียน: key ใหม่ = ทีมใหม่)
NAMES = {}              # ชื่อทีม -> key (เข้าด้วยชื่อทีมซ้ำ = ทีมเดิม)
REG_LOCK = threading.Lock()
AGENTS = {}             # team_id -> {"proc", "key", "strategy", "log"}  agent ที่ตลาดเปิดให้เอง


def agent_status(team):
    a = AGENTS.get(team)
    if not a:
        return {"running": False}
    running = a["proc"].poll() is None
    tail = []
    try:
        tail = a["log"].read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
    except OSError:
        pass
    return {"running": running, "strategy": a["strategy"] or "strategy.py ของทีม", "exit_code": a["proc"].poll(),
            "log": str(a["log"].relative_to(HERE.parent)), "tail": tail}


def start_agent(team, key, strategy=None, rules=False):
    """เปิด main.py ของทีม (ผ่าน run_agent.py) ให้เล่นเป็นทีมนี้ ไม่ต้องเปิด terminal เอง"""
    stop_agent(team)
    log = HERE / "agent-logs" / team / "console.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-u", str(HERE / "run_agent.py"), "--url", f"http://127.0.0.1:{ARGS.port}", "--key", key, "--forever"]
    if strategy:
        cmd += ["--strategy", strategy]
    if rules:
        cmd += ["--rules"]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    f = open(log, "a", encoding="utf-8")
    f.write(f"\n===== {datetime.now():%H:%M:%S} เริ่ม agent ({strategy or 'strategy.py ของทีม'}) =====\n")
    f.flush()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, cwd=str(HERE.parent), stdout=f, stderr=subprocess.STDOUT, env=env, creationflags=flags)
    AGENTS[team] = {"proc": proc, "key": key, "strategy": strategy, "log": log}


def stop_agent(team):
    a = AGENTS.pop(team, None)
    if a and a["proc"].poll() is None:
        a["proc"].terminate()


@atexit.register
def _stop_all():
    for t in list(AGENTS):
        stop_agent(t)


def strategy_names():
    d = HERE / "strategies"
    return sorted(p.stem for p in d.glob("v*.py")) if d.exists() else []


BANS_FILE = HERE / "team-bans.json"
BANNED = set()


def load_bans():
    try:
        BANNED.update(json.loads(BANS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def team_of(headers):
    t = _team_of_raw(headers)
    if t in BANNED:
        raise ApiError(403, "TEAM_REMOVED", f"Team {t} was removed from this market.", hint="Join again with a new team name.")
    return t


def _team_of_raw(headers):
    auth = headers.get("Authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not key or key.lower() in ("none", "sk-t01-change-me", "sk-t00-ใส่key-ของทีม"):
        if ARGS.require_key:
            raise ApiError(401, "UNAUTHORIZED", "Missing API key.", hint="Send header Authorization: Bearer <team key>.")
        return "T01"
    m = re.match(r"sk-(T\d{1,3})(-|$)", key)
    if m:
        return m.group(1)
    if ARGS.require_key:
        raise ApiError(401, "UNAUTHORIZED", "Unknown API key.", hint="Team keys look like sk-T05-....")
    with REG_LOCK:
        if key not in KEYS:
            used = set(KEYS.values()) | {t for g in EX.games.values() for t in g.sellers}
            n = 61
            while f"T{n}" in used:
                n += 1
            KEYS[key] = f"T{n}"
        return KEYS[key]


NAMES_FILE = HERE / "team-names.json"


def load_names():
    """ชื่อที่ตั้งผ่านปุ่ม ✎ เก็บไว้ในไฟล์ รีสตาร์ตตลาดแล้วทีมเดิม (รหัสเดิม) ได้ชื่อเดิมกลับมา"""
    try:
        for t, nm in json.loads(NAMES_FILE.read_text(encoding="utf-8")).items():
            EX.profiles.setdefault(t, {})["team_name"] = nm
    except (OSError, ValueError):
        pass


def save_names(team, name):
    try:
        cur = json.loads(NAMES_FILE.read_text(encoding="utf-8")) if NAMES_FILE.exists() else {}
    except (OSError, ValueError):
        cur = {}
    cur[team] = name
    NAMES_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")


def join(body):
    """เข้าตลาดด้วยชื่อทีม ไม่ต้องมี key: ได้ key กลับไปใช้ทั้งหน้าเว็บและ agent (TEAM_KEY)"""
    name = str((body or {}).get("team_name") or "").strip()[:40]
    if not name:
        raise ApiError(400, "INVALID_FIELD", "Type a team name.", field="team_name")
    with REG_LOCK:
        key = NAMES.get(name)
        if key is None:
            try:
                reserved = set(json.loads(NAMES_FILE.read_text(encoding="utf-8")))     # รหัสที่ตั้งชื่อไว้แล้ว (ของเพื่อน) ไม่แจกให้ทีมอื่น
            except (OSError, ValueError):
                reserved = set()
            used = set(KEYS.values()) | reserved | BANNED | {t for g in EX.games.values() for t in g.sellers} | \
                {f"T{int(m.group(1)):02d}" for k in NAMES.values() if (m := re.match(r"sk-T(\d+)", k))}
            n = 1
            while f"T{n:02d}" in used:
                n += 1
            key = f"sk-T{n:02d}-{secrets.token_hex(3)}"
            NAMES[name] = key
    team = team_of({"Authorization": "Bearer " + key})
    EX.profiles.setdefault(team, {}).update({"team_name": name, "university": str(body.get("university") or "")[:60]})
    with EX.lock:
        EX.game_for(team)
    return 200, {"team_id": team, "team_name": name, "key": key}


def rate_limit(team):
    now = time.time()
    tokens, last = BUCKETS.get(team, [10.0, now])
    tokens = min(10.0, tokens + (now - last) * 5)
    if tokens < 1:
        BUCKETS[team] = [tokens, now]
        wait = round((1 - tokens) / 5, 2)
        raise ApiError(429, "RATE_LIMITED", "More than 5 requests per second.", hint=f"Wait {wait} s and call less often.",
                       allowed={"retry_after_s": wait})
    BUCKETS[team] = [tokens - 1, now]


# ---------------------------------------------------------------- มุมมองข้อมูล

def lot_view(g, s, lot):
    in_open = s.kg_in_open(lot["lot_id"])
    return {"lot_id": lot["lot_id"], "kg_total": lot["kg_total"], "kg_remaining": lot["kg_remaining"],
            "kg_in_open_offers": in_open, "kg_available": max(0, lot["kg_remaining"] - in_open),
            "origin": lot["origin"], "process": lot["process"], "sample": dict(lot["sample"])}


def rfq_view(r):
    return {k: v for k, v in r.items() if not k.startswith("_")}


def offer_view(o):
    return {k: o[k] for k in ("offer_id", "round_id", "rfq_id", "lot_id", "kg", "price_per_kg", "status", "filled_kg")}


def round_view(g):
    rnd = g.round()
    if rnd is None:
        return None
    return {"round_id": rnd["round_id"], "kind": rnd["kind"], "number": rnd["number"], "of": g.cfg["rounds"],
            "counts_for_score": rnd["kind"] == "real", "calendar": g.cal_entry(rnd)["label"]}


def standings(s):
    return {"revenue_thb": round(s.revenue, 2), "kg_sold": s.kg_sold, "kg_remaining": s.kg_remaining(),
            "kg_total": s.kg_total(), "avg_price_per_kg": round(s.revenue / s.kg_sold, 2) if s.kg_sold else None}


def state_view(g, s, now):
    show_rfqs = g.phase in ("ROUND_OPEN", "ROUND_CLEARING")
    return {"market": g.name, "team_id": s.id, "phase": g.phase, "paused": g.paused,
            "seconds_left": g.seconds_left(now), "phase_ends_at": g.phase_ends_at, "server_time": round(now, 3),
            "round": round_view(g),
            "lots": [lot_view(g, s, l) for l in s.lots.values()] if g.phase != "WAITING" else [],
            "rfqs": [rfq_view(r) for r in g.rfqs] if show_rfqs else [],
            "my_open_offers": [offer_view(o) for o in s.open_offers()],
            "round_capacity_kg": g.capacity_kg(s),
            "capacity_left_kg": g.capacity_left(s) if g.phase == "ROUND_OPEN" else g.capacity_kg(s),
            "appraisal_submitted": s.appraisal is not None,
            "last_results": s.results.get(s.last_round_id) if s.last_round_id else None,
            "standings": standings(s), "salvage_ratio": g.rules["salvage_ratio"]}


def board_rows(g, s_me=None, numbers=False):
    rows = []
    ranked = g.ranking()
    live_rank = {x.id: i for i, x in enumerate(ranked, 1)}
    order = ranked
    frozen = g.frozen_board is not None and g.phase != "CLOSED"
    if frozen and not numbers:
        order = [g.sellers[i] for i in g.frozen_board if i in g.sellers] + [x for x in ranked if x.id not in g.frozen_board]
    for i, s in enumerate(order, 1):
        p = EX.profiles.get(s.id, {})
        row = {"rank": i, "team_id": s.id, "team_name": p.get("team_name") or s.name,
               "university": p.get("university") or s.university, "is_bot": s.is_bot, "is_me": s is s_me,
               "owner": None if s.is_bot else ("friend" if p.get("origin") not in (None, "local") else "ours"),
               "ip": p.get("origin") if p.get("origin") not in (None, "local") else None}
        if s.is_bot and numbers:
            row["policy"] = s.policy
        if numbers or g.phase == "CLOSED":
            st = standings(s)
            row.update({"live_rank": live_rank[s.id], "pr_now": round(g.live_pr(s), 4), "revenue_thb": st["revenue_thb"],
                        "kg_sold": st["kg_sold"], "kg_total": st["kg_total"], "avg_price_per_kg": st["avg_price_per_kg"],
                        "appraised": s.appraisal is not None})
            ch = g.rank_change(s.id)
            if ch:
                row.update({"rank_delta": ch["rank_delta"], "pr_delta": ch["pr_delta"], "delta_round": ch["round_id"]})
        if g.phase == "CLOSED" and s.score:
            row.update({k: s.score[k] for k in ("pr", "pr_score", "appraisal_score", "total", "mape")})
        rows.append(row)
    return {"phase": g.phase, "frozen": frozen, "rows": rows}


def leaderboard(g, s_me):
    """เหมือนตลาดจริง: ระหว่างแข่งมีแต่อันดับ ไม่มีตัวเลข · หยุดอัปเดตหลังรอบ freeze"""
    return board_rows(g, s_me, numbers=False)


# ---------------------------------------------------------------- เส้นทาง API

def api(method, path, query, body, headers):
    now = time.time()
    if path == "/v1/version":
        return 200, {"api": "v1", "server": VERSION, "mode": "sandbox" if EX.cfg["reveal"] else "competition",
                     "preset": ARGS.preset, "server_time": round(now, 3), "trade_page": True,
                     "market_mode": EX.cfg["mode"], "manual_disabled": bool(EX.cfg["manual_disabled"]),
                     "reveal": EX.cfg["reveal"], "bots": EX.cfg["bots"], "speed": EX.cfg.get("speed", 1),
                     "auto_skip": bool(EX.cfg.get("auto_skip")),
                     "lan_url": f"http://{lan_ip()}:{ARGS.port}" if ARGS.host == "0.0.0.0" else None}
    if (method, path) == ("POST", "/v1/join"):
        return join(body)
    team = team_of(headers)
    rate_limit(team)
    with EX.lock:
        g = EX.game_for(team, now)
        s = g.seller(team)
        manual = headers.get("X-Client") == "trade-page"

        def no_manual():
            if manual and EX.cfg["manual_disabled"]:
                raise ApiError(403, "MANUAL_DISABLED", "Manual orders from the web page are disabled in this market.",
                               hint="Let your agent send it.")

        route = (method, path)
        if route == ("GET", "/v1/state"):
            return 200, state_view(g, s, now)
        if route == ("GET", "/v1/calendar"):
            return 200, {"calendar": g.calendar, "rules": g.rules}
        if route == ("GET", "/v1/lots"):
            return 200, {"lots": [lot_view(g, s, l) for l in s.lots.values()]}
        if route == ("GET", "/v1/rfqs"):
            rv = round_view(g)
            return 200, {"round_id": rv and rv["round_id"], "rfqs": [rfq_view(r) for r in g.rfqs]
                         if g.phase in ("ROUND_OPEN", "ROUND_CLEARING") else []}
        if route == ("GET", "/v1/standings"):
            return 200, standings(s)
        if route == ("GET", "/v1/leaderboard"):
            return 200, leaderboard(g, s)
        if route == ("GET", "/v1/profile"):
            p = EX.profiles.get(team, {})
            return 200, {"team_id": team, "team_name": p.get("team_name"), "university": p.get("university")}
        if route == ("POST", "/v1/profile"):
            if not isinstance(body, dict):
                raise ApiError(400, "INVALID_FIELD", "Body must be an object.")
            p = EX.profiles.setdefault(team, {})
            for k in ("team_name", "university"):
                if k in body:
                    p[k] = str(body[k])[:60]
            return 200, {"team_id": team, **p}

        if route == ("POST", "/v1/appraisals"):
            no_manual()
            if g.phase != "APPRAISAL":
                raise ApiError(409, "WRONG_PHASE", f"Appraisals are accepted only in APPRAISAL; the market is in {g.phase}.",
                               allowed={"phase": g.phase, "phase_ends_at": g.phase_ends_at})
            if s.appraisal is not None:
                raise ApiError(409, "APPRAISAL_ALREADY_SUBMITTED", "Your team already sent its appraisal.",
                               hint="Nothing to do: it can be sent only once.")
            items = body.get("items") if isinstance(body, dict) else None
            if not isinstance(items, list) or not items:
                raise ApiError(400, "INVALID_FIELD", "Body must be {\"items\": [...]} with one item per lot.", field="items")
            rec = []
            for i, it in enumerate(items):
                lid = it.get("lot_id") if isinstance(it, dict) else None
                if lid not in s.lots:
                    other = any(lid in x.lots for x in g.sellers.values())
                    raise ApiError(404, "NOT_YOUR_LOT" if other else "UNKNOWN_LOT", f"items[{i}]: lot {lid} is not yours.",
                                   field=f"items[{i}].lot_id", allowed={"lot_ids": list(s.lots)})
                v = it.get("est_value_per_kg")
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 < v <= 1000):
                    raise ApiError(400, "INVALID_FIELD", f"items[{i}]: est_value_per_kg must be a number in (0, 1000].",
                                   field=f"items[{i}].est_value_per_kg")
                rec.append({"lot_id": lid, "grade": it.get("grade"), "est_value_per_kg": round(float(v), 2)})
            s.appraisal = {"submitted_at": round(now, 3), "items": rec, "by_lot": {r["lot_id"]: r["est_value_per_kg"] for r in rec}}
            return 201, {"submitted_at": s.appraisal["submitted_at"], "items": rec}
        if route == ("GET", "/v1/appraisals"):
            if not s.appraisal:
                raise ApiError(409, "NOT_AVAILABLE", "No appraisal sent yet.")
            return 200, {"submitted_at": s.appraisal["submitted_at"], "items": s.appraisal["items"]}

        if route == ("POST", "/v1/offers"):
            no_manual()
            errors, ok = g.place(s, [body])
            if errors:
                e = errors[0]
                status = 404 if e["code"].startswith("UNKNOWN") or e["code"] == "NOT_YOUR_LOT" else 400
                raise ApiError(status, e["code"], e["message"], field=e["field"], allowed=e["allowed"],
                               hint=_hint(e))
            made, _ = g.commit(s, ok)
            return 201, offer_view(made[0])
        if route == ("POST", "/v1/offers:batch"):
            no_manual()
            if not isinstance(body, dict) or not isinstance(body.get("offers"), list):
                raise ApiError(400, "INVALID_FIELD", "Body must be {\"offers\": [...], \"replace_existing\": true}.", field="offers")
            replace = bool(body.get("replace_existing", True))
            errors, ok = g.place(s, body["offers"], replace)
            if errors:
                raise ApiError(400, "BATCH_REJECTED", f"{len(errors)} of {len(body['offers'])} offers are invalid; nothing was saved.",
                               hint="Fix the offers listed in allowed.errors and send the whole batch again.",
                               allowed={"errors": errors})
            made, cancelled = g.commit(s, ok, replace)
            return 201, {"round_id": g.round()["round_id"], "offers": [offer_view(o) for o in made], "cancelled": cancelled}
        if route == ("GET", "/v1/offers"):
            rv = round_view(g)
            return 200, {"round_id": rv and rv["round_id"], "offers": [offer_view(o) for o in s.offers]}
        m = re.fullmatch(r"/v1/offers/([A-Za-z0-9]+)", path)
        if m and method in ("PUT", "DELETE"):
            no_manual()
            o = next((x for x in s.offers if x["offer_id"] == m.group(1)), None)
            if o is None:
                raise ApiError(404, "UNKNOWN_OFFER", f"Your team has no offer {m.group(1)} in this round.",
                               hint="See GET /v1/offers.")
            if o["status"] != "OPEN" or g.phase != "ROUND_OPEN":
                raise ApiError(409, "ROUND_CLOSED", "This offer is no longer open.", hint="Send a new offer next round.")
            g.check_phase()
            if method == "DELETE":
                o["status"] = "CANCELLED"
                return 200, offer_view(o)
            new = {"rfq_id": o["rfq_id"], "lot_id": o["lot_id"], "kg": body.get("kg", o["kg"]),
                   "price_per_kg": body.get("price_per_kg", o["price_per_kg"])}
            used = {}
            for x in s.open_offers():
                if x is not o:
                    used[x["lot_id"]] = used.get(x["lot_id"], 0) + x["kg"]
            cap = g.capacity_kg(s)
            room = None if cap is None else cap - sum(x["kg"] for x in s.open_offers() if x is not o)
            e = g.offer_errors(s, new, used, room)
            if e:
                raise e
            o["kg"], o["price_per_kg"] = int(new["kg"]), round(float(new["price_per_kg"]), 2)
            return 200, offer_view(o)
        if path.startswith("/v1/offers") and not m:
            pass

        if route == ("GET", "/v1/results"):
            rid = (query.get("round") or [None])[0] or s.last_round_id
            if not rid or rid not in s.results:
                raise ApiError(409, "NOT_AVAILABLE", f"No cleared results for {rid or 'any round'} yet.",
                               hint="Results appear after ROUND_CLEARING ends.", allowed={"rounds": list(s.results)})
            return 200, s.results[rid]

        if route == ("POST", "/v1/sandbox/reset"):
            if not EX.cfg["reveal"]:
                raise ApiError(409, "NOT_AVAILABLE", "Reset is only for the practice market.")
            g = EX.reset(team)
            return 200, state_view(g, g.seller(team), now)
        if route == ("GET", "/v1/sandbox/reveal"):
            if not EX.cfg["reveal"]:
                raise ApiError(409, "NOT_AVAILABLE", "Reveal is only for the practice market.")
            if g.phase != "CLOSED":
                raise ApiError(409, "NOT_AVAILABLE", "The true values are shown only after the market closes.",
                               hint="Wait for phase CLOSED.", allowed={"phase": g.phase})
            lots = []
            for l in s.lots.values():
                est = s.appraisal["by_lot"].get(l["lot_id"]) if s.appraisal else None
                gr = next((i.get("grade") for i in s.appraisal["items"] if i["lot_id"] == l["lot_id"]), None) if s.appraisal else None
                lots.append({"lot_id": l["lot_id"], "kg_total": l["kg_total"], "kg_remaining": l["kg_remaining"],
                             "sample": l["sample"], "true": l["true"], "true_grade": grade(g.rules, l["true"]["defect_pct"])[0],
                             "true_value_per_kg": round(l["tv"], 2),
                             "your_estimate": None if est is None else {"grade": gr, "est_value_per_kg": est}})
            return 200, {"lots": lots, "score": s.score}

        if path.startswith("/v1/"):
            known = {"/v1/state", "/v1/calendar", "/v1/lots", "/v1/rfqs", "/v1/standings", "/v1/leaderboard", "/v1/profile",
                     "/v1/appraisals", "/v1/offers", "/v1/offers:batch", "/v1/results", "/v1/sandbox/reset",
                     "/v1/sandbox/reveal", "/v1/handin"}
            if path in known or m:
                raise ApiError(405, "METHOD_NOT_ALLOWED", f"{method} is not allowed on {path}.", hint="See the endpoint table.")
        raise ApiError(404, "UNKNOWN_ENDPOINT", f"No endpoint {method} {path}.", hint="Every path starts with /v1.")


def _hint(e):
    return {"EXCEEDS_REMAINING": "Reduce kg or cancel another offer on that lot.",
            "OVER_ROUND_CAPACITY": "Check capacity_left_kg before sending.",
            "NOT_MULTIPLE_OF_BAG": "Use a value in allowed.nearest.",
            "UNKNOWN_RFQ": "Use an rfq_id from state.rfqs of the current round."}.get(e["code"])



LOOP = {"on": False, "status": "idle", "results": [], "log": [], "winner": None}
LOOP_FILE = HERE / "loop-results.json"


def _self(path, body=None):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{ARGS.port}{path}", data=json.dumps(body or {}).encode("utf-8"), method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _loop_save():
    LOOP_FILE.write_text(json.dumps({"results": LOOP["results"], "winner": LOOP["winner"], "status": LOOP["status"]},
                                    ensure_ascii=False, indent=1), encoding="utf-8")


def _loop_worker(cfg):
    """เล่นซ้ำอัตโนมัติ: เริ่มเกม → รอจบ → เก็บ PR ของทีม → เปลี่ยนกลยุทธ์ → เริ่มใหม่ (ไม่ต้องพึ่งหน้าเว็บ/เบราว์เซอร์)
    เกมไหนพัง จะบันทึก error แล้วไปเกมต่อไป (ติดกัน 5 เกมถึงจะหยุด) เพื่อให้รันทั้งคืนได้"""
    name, order, total = cfg["team_name"], cfg["strategies"], int(cfg["total"])

    def play(i, st, team, key):
        LOOP["status"] = f"เกม {i + 1}/{total} · {st}"
        _self("/admin/reset")
        _self("/admin/start")
        _self("/admin/agent", {"team": team, "key": key, "action": "start", "strategy": st, "rules": True})
        for _ in range(40):                                      # ข้ามเตรียมตัว/ประเมิน ไปรอบเสนอราคา
            time.sleep(2.5)
            ph = _self("/admin/state")["phase"]
            if ph == "ROUND_OPEN":
                break
            if ph in ("SETUP", "APPRAISAL", "WAITING"):
                _self("/admin/skip")
        t0 = time.time()
        while LOOP["on"] and time.time() - t0 < 2400:
            time.sleep(4)
            s = _self("/admin/state", {"numbers": True})
            if s["phase"] == "CLOSED":
                rows = (s.get("board") or {}).get("rows") or []
                me = next((r for r in rows if r["team_id"] == team), None)
                if not me:
                    raise RuntimeError(f"ไม่พบทีม {team} ในอันดับ")
                LOOP["results"].append({"game": i + 1, "strategy": st, "pr": me["pr"], "rank": me["rank"], "total": me["total"],
                                        "kg_sold": me["kg_sold"], "best_pr": rows[0]["pr"], "at": datetime.now().isoformat(timespec="seconds")})
                _loop_save()
                return
        if LOOP["on"]:
            raise RuntimeError("เกมไม่จบภายใน 40 นาที")

    try:
        j = _self("/v1/join", {"team_name": name})
        team, key = j["team_id"], j["key"]
        bad = 0
        for i in range(total):
            if not LOOP["on"]:
                break
            try:
                play(i, order[i % len(order)], team, key)
                bad = 0
            except Exception as e:  # noqa: BLE001
                bad += 1
                LOOP["log"].append(f"เกม {i + 1}: {e!r}")
                if bad >= 5:
                    raise
                time.sleep(5)
        by = {}
        for r in LOOP["results"]:
            by.setdefault(r["strategy"], []).append(r["pr"])
        if by:
            LOOP["winner"] = max(by, key=lambda k: sum(by[k]) / len(by[k]))
            _self("/admin/agent", {"team": team, "key": key, "action": "start", "strategy": LOOP["winner"], "rules": True})
        LOOP["status"] = "เสร็จ" if LOOP["on"] else "หยุดโดยผู้ใช้"
    except Exception as e:  # noqa: BLE001
        LOOP["log"].append(repr(e))
        LOOP["status"] = f"error: {e!r}"
    LOOP["on"] = False
    _loop_save()


def admin(path, body):
    """ปุ่มควบคุมตลาด (ไม่มีในตลาดจริง) · กดได้จากเครื่องที่รันตลาดเท่านั้น"""
    body = body or {}
    team = body.get("team") or "T01"
    with EX.lock:
        now = time.time()
        if EX.cfg["mode"] == "shared":       # ไม่เพิ่ม "ทีมผี" เข้าตลาดรวมตอนกดปุ่ม
            g = EX.games.get("shared") or EX.games.setdefault("shared", EX._new_game("shared"))
        else:
            g = EX.game_for(team, now)
        g.tick(now)
        if path == "/admin/skip":
            g.skip(now)
        elif path == "/admin/pause":
            g.set_paused(not g.paused, now)
        elif path == "/admin/start":
            g.start(now)
        elif path == "/admin/reset":
            if g.phase not in ("WAITING", "CLOSED") and g.rank_hist:      # เกมค้างกลางทาง → เก็บผลไว้ก่อนล้าง
                try:
                    g.write_log(partial=True)
                except Exception as e:      # noqa: BLE001
                    print(f"[log] เก็บผลเกมค้างไม่สำเร็จ: {e!r}", flush=True)
            g = EX.reset(None if EX.cfg["mode"] == "shared" else team)
        elif path == "/admin/set":
            for k in ("speed", "auto_skip", "manual_disabled"):
                if k in body:
                    EX.cfg[k] = body[k]
        elif path == "/admin/agent":
            t, key = body.get("team"), body.get("key")
            if not t or not key or team_of({"Authorization": "Bearer " + key}) != t:
                raise ApiError(400, "INVALID_FIELD", "Need team and its key.")
            if body.get("action") == "stop":
                stop_agent(t)
            elif body.get("auto") and (agent_status(t) or {}).get("running"):
                pass        # เข้าทีมซ้ำจากหน้าเว็บ (ติ๊ก "ให้ agent เล่นอัตโนมัติ") ต้องไม่ไปทับ agent ที่ตั้งกลยุทธ์ไว้แล้วและกำลังรันอยู่
            else:
                st = body.get("strategy") or None
                if st and st not in strategy_names():
                    raise ApiError(400, "INVALID_FIELD", f"Unknown strategy {st}.", allowed={"strategies": strategy_names()})
                start_agent(t, key, st, bool(body.get("rules")))
            return 200, {"team": t, "agent": agent_status(t)}
        elif path == "/admin/loop":
            act = body.get("action", "status")
            if act == "start" and not LOOP["on"]:
                LOOP.update(on=True, results=[], log=[], winner=None, status="เริ่ม")
                threading.Thread(target=_loop_worker, args=({"team_name": body.get("team_name", "เราดูดเม็ด2"),
                                                             "strategies": body.get("strategies") or ["v16_market"],
                                                             "total": body.get("total", 40)},), daemon=True).start()
            elif act == "stop":
                LOOP["on"] = False
            return 200, dict(LOOP)
        elif path == "/admin/remove":
            t = str(body.get("team") or "")
            if not re.match(r"^T\d{1,3}$", t):
                raise ApiError(400, "INVALID_FIELD", "Need a team id like T05.")
            BANNED.add(t)
            BANS_FILE.write_text(json.dumps(sorted(BANNED)), encoding="utf-8")
            stop_agent(t)
            for gg in EX.games.values():
                gg.sellers.pop(t, None)
            EX.profiles.pop(t, None)
            if g.phase in ("WAITING", "CLOSED") or True:
                g.sellers.pop(t, None)
        elif path == "/admin/rename":
            t, nm = body.get("team"), str(body.get("name") or "").strip()[:40]
            if not t or not nm or not re.match(r"^T\d{1,3}$", str(t)):
                raise ApiError(400, "INVALID_FIELD", "Need an existing team and a name.")
            EX.profiles.setdefault(t, {})["team_name"] = nm
            save_names(t, nm)
        elif path == "/admin/agent_status":
            return 200, {"team": team, "agent": agent_status(team), "strategies": strategy_names()}
        elif path not in ("/admin/info", "/admin/state"):
            raise ApiError(404, "UNKNOWN_ENDPOINT", path)
        out = {"preset": ARGS.preset, "mode": EX.cfg["mode"], "phase": g.phase, "paused": g.paused,
               "bots": sum(1 for s in g.sellers.values() if s.is_bot),
               "teams": [{"team_id": s.id, "team_name": EX.profiles.get(s.id, {}).get("team_name") or s.id,
                          "appraised": s.appraisal is not None, "sent_this_round": bool(s.offers),
                          "owner": "friend" if EX.profiles.get(s.id, {}).get("origin") not in (None, "local") else "ours",
                          "ip": EX.profiles.get(s.id, {}).get("origin") if EX.profiles.get(s.id, {}).get("origin") not in (None, "local") else None,
                          "agent": (AGENTS.get(s.id) or {}).get("proc") is not None and AGENTS[s.id]["proc"].poll() is None}
                         for s in g.humans()],
               "strategies": strategy_names(),
               "manual_disabled": bool(EX.cfg["manual_disabled"]), "reveal": EX.cfg["reveal"],
               "speed": EX.cfg.get("speed", 1), "auto_skip": bool(EX.cfg.get("auto_skip")),
               "lan_url": f"http://{lan_ip()}:{ARGS.port}" if ARGS.host == "0.0.0.0" else None,
               "buyers": [{"buyer": b["buyer"], "buyer_type": b["buyer_type"], "ceiling": b["ceiling"]} for b in g.buyers]
               if ARGS.show_secrets else None}
        if path == "/admin/state":
            out.update({"round": round_view(g), "seconds_left": g.seconds_left(now), "calendar": g.calendar,
                        "rounds_total": g.cfg["rounds"], "practice_rounds": g.cfg["practice_rounds"],
                        "freeze_after": g.cfg["leaderboard_freeze_after_round"],
                        "board": board_rows(g, None, numbers=bool(body.get("numbers"))),
                        "last_summary": next((s.results.get(s.last_round_id) for s in g.sellers.values()
                                              if s.last_round_id), None)})
        return 200, out


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
            so.connect(("10.255.255.255", 1))
            return so.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def handin(raw, headers):
    team = team_of(headers)
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = z.namelist()
    except zipfile.BadZipFile:
        raise ApiError(400, "INVALID_FIELD", "The body is not a zip file.", hint="Run python pack.py first.")
    if len(raw) > 20 * 1024 * 1024:
        raise ApiError(400, "INVALID_FIELD", "The zip is larger than 20 MB.")
    out = HERE / "handins"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"{team}-{stamp}.zip").write_bytes(raw)
    return 201, {"received_local": datetime.now().strftime("%H:%M:%S"), "files": len(names), "team_id": team,
                 "has_decisions_log": any(n.endswith("logs/decisions.jsonl") for n in names), "saved_as": f"handins/{team}-{stamp}.zip"}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "CoffeeMock/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        if ARGS.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    def _send(self, status, data, ctype="application/json; charset=utf-8", extra=None):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Client")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        u = urlparse(self.path)
        path, query = u.path.rstrip("/") or "/", parse_qs(u.query)
        raw = self._body()
        try:
            if path == "/favicon.ico":
                return self._send(204, b"")
            if method == "GET" and path in ("/", "/trade", "/handin", "/leaderboard", "/board"):
                page = {"/handin": "handin.html", "/leaderboard": "leaderboard.html", "/board": "leaderboard.html"}.get(path, "trade.html")
                return self._send(200, (HERE / "static" / page).read_bytes(), "text/html; charset=utf-8")
            if path.startswith("/llm"):
                status, data, extra = fake_llm.handle(method, path[4:] or "/", raw, self.headers, ARGS)
                return self._send(status, data, extra=extra)
            if path.startswith("/admin/") and method == "POST":
                body = json.loads(raw or b"{}")
                local = self.client_address[0] in ("127.0.0.1", "::1") or ARGS.open_admin
                if not local:
                    if path != "/admin/state":
                        raise ApiError(403, "ADMIN_LOCAL_ONLY", "Market controls work only on the computer running the mock market.")
                    body["numbers"] = True   # เปิดแดชบอร์ดให้ทุกคนในวงแลนดูได้เต็ม (PR/รายได้/±รอบ) ไม่ต้องใช้ key · ดูอย่างเดียว
                status, data = admin(path, body)
                data["is_admin"] = local
                return self._send(status, data)
            if path == "/v1/handin" and method == "POST":
                status, data = handin(raw, self.headers)
                return self._send(status, data)
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                raise ApiError(400, "INVALID_FIELD", "The body is not valid JSON.")
            if self.headers.get("Authorization") and path != "/v1/join":     # จำว่าทีมนี้เข้ามาจากเครื่องไหน: เครื่องเรา = ของเรา · เครื่องอื่น = เพื่อน
                try:
                    pr = EX.profiles.setdefault(team_of(self.headers), {})
                    pr.setdefault("origin", "local" if self.client_address[0] in ("127.0.0.1", "::1") else self.client_address[0])
                except ApiError:
                    pass
            status, data = api(method, path, query, body, self.headers)
            self._send(status, data)
        except ApiError as e:
            extra = {"Retry-After": str(max(1, round(e.allowed["retry_after_s"])))} if e.code == "RATE_LIMITED" else None
            self._send(e.status, e.body(), extra=extra)
        except Exception as e:      # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._send(500, {"error": {"code": "INTERNAL", "message": f"{type(e).__name__}: {e}", "field": None,
                                       "hint": "Bug in the mock market — see the server console.", "allowed": None}})


def ticker():
    while True:
        try:
            EX.tick_all()
        except Exception:           # noqa: BLE001
            import traceback
            traceback.print_exc()
        time.sleep(0.25)


def main(argv=None):
    global EX, ARGS
    p = argparse.ArgumentParser(description="Local mock of the TESA coffee market")
    p.add_argument("--preset", default=os.environ.get("MOCK_PRESET", "fast"), choices=list(PRESETS))
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = ให้เครื่องอื่นในวงแลนเข้าได้")
    p.add_argument("--port", type=int, default=int(os.environ.get("MOCK_PORT", "9000")))
    p.add_argument("--bots", type=int, default=None, help="จำนวนบอตคู่แข่ง (ค่าเริ่มต้นตาม preset: 19 · contest 55 · stage3 100)")
    p.add_argument("--speed", type=float, default=1.0, help="เร่งเวลา เช่น 10 = รอบ 6 นาทีเหลือ 36 วิ (ปรับในหน้าอันดับได้)")
    p.add_argument("--auto-skip", action="store_true", help="ทุกทีมส่งครบแล้วข้ามไปช่วงถัดไปเลย")
    p.add_argument("--no-manual", action="store_true", help="ห้ามส่งจากหน้าเว็บ (เหมือนวันแข่ง)")
    p.add_argument("--lan", action="store_true", help="ให้เพื่อนในวง Wi-Fi เดียวกันต่อ agent เข้ามาได้ (= --host 0.0.0.0)")
    p.add_argument("--open-admin", action="store_true", help="ให้เครื่องอื่นกดปุ่มควบคุมตลาดได้ด้วย (เปิดให้อัตโนมัติเมื่อใช้ --lan)")
    p.add_argument("--no-open-admin", action="store_true", help="ใช้คู่กับ --lan: ให้เพื่อนดูอย่างเดียว กดควบคุมตลาดไม่ได้")
    p.add_argument("--no-browser", action="store_true", help="ไม่ต้องเปิดหน้าเว็บอัตโนมัติ")
    p.add_argument("--seed", type=int, default=None, help="ใส่เลขเพื่อให้ล็อต/ผู้ซื้อออกมาเหมือนเดิมทุกครั้ง")
    p.add_argument("--mode", choices=["private", "shared"], help="private = ตลาดแยกทีม · shared = ทุก key อยู่ตลาดเดียวกัน")
    p.add_argument("--open-s", type=float, help="ความยาวรอบเปิดรับ (วินาที)")
    p.add_argument("--appraisal-s", type=float, help="ความยาวช่วงประเมิน (วินาที)")
    p.add_argument("--rounds", type=int)
    p.add_argument("--require-key", action="store_true", help="บังคับต้องมี key รูปแบบ sk-Txx")
    p.add_argument("--show-secrets", action="store_true", help="หน้าเว็บโชว์เพดานลับของผู้ซื้อ (ไว้ debug)")
    p.add_argument("--llm-fail-rate", type=float, default=0.05, help="โอกาสที่ LLM ปลอมตอบพัง/ช้าเกิน (0–1)")
    p.add_argument("--llm-latency", type=float, default=1.0, help="ตัวคูณความช้าของ LLM ปลอม (0 = ตอบทันที)")
    p.add_argument("--llm-upstream", default=os.environ.get("MOCK_LLM_UPSTREAM"),
                   help="ส่งต่อไป LLM จริง เช่น http://localhost:11434/v1 (Ollama) แทนการตอบปลอม")
    p.add_argument("--llm-model", default=os.environ.get("MOCK_LLM_MODEL", "qwen3.5:4b"), help="model ของ upstream")
    p.add_argument("--verbose", action="store_true")
    ARGS = p.parse_args(argv)
    if ARGS.lan and not ARGS.no_open_admin:
        ARGS.open_admin = True              # เพื่อนในวงแลนกดปุ่มควบคุมตลาดได้เหมือนเจ้าของเครื่อง
    if ARGS.lan:
        ARGS.host = "0.0.0.0"
    cfg = dict(PRESETS[ARGS.preset])
    cfg["bots"] = ARGS.bots if ARGS.bots is not None else cfg.get("bots", 55 if ARGS.preset == "contest" else 19)
    cfg["speed"], cfg["auto_skip"] = ARGS.speed, ARGS.auto_skip
    cfg["name"] = f"{ARGS.preset} (จำลอง)"
    if ARGS.no_manual:
        cfg["manual_disabled"] = True
    if ARGS.mode:
        cfg["mode"] = ARGS.mode
    if ARGS.open_s:
        cfg["open_s"] = ARGS.open_s
    if ARGS.appraisal_s:
        cfg["appraisal_s"] = ARGS.appraisal_s
    if ARGS.rounds:
        cfg["rounds"] = ARGS.rounds
    import engine as _engine_mod                          # noqa: E402
    _engine_mod.TEAM_META = lambda team_id: {"strategy": (AGENTS.get(team_id) or {}).get("strategy")}
    EX = Exchange(cfg, ARGS.seed)
    load_names()
    load_bans()
    threading.Thread(target=ticker, daemon=True).start()
    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    srv.daemon_threads = True
    base = f"http://localhost:{ARGS.port}"
    print(f"ตลาดจำลอง ({ARGS.preset}, {cfg['mode']}, บอต {cfg['bots']}) พร้อมแล้ว")
    print(f"  หน้าเว็บ      {base}/trade   ·   จออันดับ {base}/leaderboard")
    if ARGS.host == "0.0.0.0":
        print(f"  เพื่อนในวง Wi-Fi: MARKET_URL=http://{lan_ip()}:{ARGS.port}  (Windows อาจถามเรื่อง Firewall ให้กด Allow)")
    print(f"  MARKET_URL    {base}")
    print(f"  LLM_BASE_URL  {base}/llm/v1   (model อะไรก็ได้ · " + ("ส่งต่อ " + ARGS.llm_upstream if ARGS.llm_upstream else "ตอบปลอม") + ")")
    print("  หยุด: Ctrl+C", flush=True)
    if not ARGS.no_browser:
        threading.Timer(0.8, lambda: (webbrowser.open(f"{base}/leaderboard"), webbrowser.open(f"{base}/trade"))).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv


if __name__ == "__main__":
    main()
