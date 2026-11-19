"""v16 = v15 + อ่านราคาตลาดของแต่ละผู้ซื้อทุกรอบ แล้วขยับราคาตาม
หลังจบทุกรอบ: ดูราคาที่ผู้ขายรายอื่นเสนอให้ผู้ซื้อแต่ละราย (price_avg / price_max ใน market_summary)
- ถ้ารอบก่อนเราแพ้ (OUTBID) ให้ผู้ซื้อรายนี้ทั้งหมด และราคาตลาดต่ำกว่าเรา → ขยับลงเข้าหาราคาตลาด (ไม่เกิน STEP_CAP ต่อรอบ)
- ถ้ารอบก่อนเราขายได้หมด (ACCEPTED) และตลาดเสนอสูงกว่าเรา → ขยับขึ้นเข้าหา price_max แต่ไม่เกินเพดานที่เรียนรู้ (hi - CAP_MARGIN)
"""
import json
import os
from pathlib import Path
import _base
_base.inherit(globals())
exec(compile(Path(__file__).with_name("v15_buyer.py").read_text(encoding="utf-8"), "v15", "exec"), globals())

W_DOWN, W_UP, STEP_CAP, OFF = 0.5, 0.0, 0.03, 0.0   # W_UP=0: ตามตลาดลงอย่างเดียว (ตามขึ้นทำให้แย่ลงในการทดสอบ)
for _k, _v in json.loads(os.environ.get("MOCK_TUNE", "{}")).items():
    globals()[_k] = _v
_buyer_rule_plan = rule_plan


def market_view(memory, mean_tv):
    """{ผู้ซื้อ: (สถานะเราในรอบล่าสุดที่เสนอ, markup ที่เราเสนอ, ราคาเฉลี่ยตลาด÷TV, ราคาสูงสุดตลาด÷TV)}"""
    view = {}
    if memory is None or not mean_tv:
        return view
    for rnd in getattr(memory, "rounds", []):
        if rnd.get("kind") == "practice":
            continue
        sent = {(o["rfq_id"], o["lot_id"]): o for o in getattr(memory, "sent", {}).get(rnd["round_id"], [])}
        summ = {r["rfq_id"]: r for r in rnd.get("market_summary", [])}
        by = {}
        for res in rnd["my_offers"]:
            o, s = sent.get((res["rfq_id"], res["lot_id"])), summ.get(res["rfq_id"])
            if o and s and s.get("price_avg"):
                by.setdefault(s["buyer"], []).append((res["status"], o["markup"], s["price_avg"] / mean_tv, s["price_max"] / mean_tv))
        for b, rows in by.items():
            sts = {x[0] for x in rows}
            view[b] = ("DOWN" if sts <= {"OUTBID"} else "UP" if sts <= {"ACCEPTED"} else "HOLD",
                       sum(x[1] for x in rows) / len(rows), sum(x[2] for x in rows) / len(rows), max(x[3] for x in rows))
    return view


def rule_plan(state, ctx, cands):
    plan = _buyer_rule_plan(state, ctx, cands)
    tvs = [v for v in (ctx.tv or {}).values() if v]
    mean_tv = sum(tvs) / len(tvs) if tvs else 0
    view = market_view(ctx.memory, mean_tv)
    _, hi = buyer_bounds(ctx.memory)
    who = {c["rfq_id"]: c["buyer"] for c in cands}
    for p in plan:
        b = who.get(p["rfq_id"])
        if b not in view:
            continue
        mode, mine, avg, mx = view[b]
        m = p["markup"]
        if mode == "DOWN" and avg + OFF < m:
            m -= min(STEP_CAP, W_DOWN * (m - (avg + OFF)))
        elif mode == "UP" and mx > m:
            m += min(STEP_CAP, W_UP * (mx - m))
        if b in hi:
            m = min(m, hi[b] - CAP_MARGIN)
        p["markup"] = round(min(max(m, MIN_MARKUP), MAX_MARKUP), 3)
    return plan
