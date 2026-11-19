"""v15 = v14_best + เรียนรู้เพดานแยกรายผู้ซื้อ (ไม่ใช่รายประเภท)
- ผู้ซื้อแต่ละรายมีเพดานลับของตัวเอง: เจอ ABOVE_LIMIT ที่ markup m → ราคาที่เสนอให้คนนี้ต้อง < m - CAP_MARGIN
- ผู้ซื้อที่เคยรับราคาเราแล้ว (ACCEPTED ที่ markup a) และยังไม่เคยเจอเพดาน → ลองขยับเป็น a + STEP ได้ (ไม่เกิน RAISE_MAX เหนือราคาตามประเภท)
"""
import json
import os
from pathlib import Path
import _base
_base.inherit(globals())
exec(compile(Path(__file__).with_name("v14_best.py").read_text(encoding="utf-8"), "v14", "exec"), globals())

CAP_MARGIN, STEP, RAISE_MAX = 0.012, 0.015, 0.06
for _k, _v in json.loads(os.environ.get("MOCK_TUNE", "{}")).items():
    globals()[_k] = _v
_type_rule_plan = rule_plan


def buyer_bounds(memory):
    """{ชื่อผู้ซื้อ: [markup สูงสุดที่เคยขายได้, markup ต่ำสุดที่เจอ ABOVE_LIMIT]}"""
    lo, hi = {}, {}
    if memory is None:
        return lo, hi
    for rnd in getattr(memory, "rounds", []):
        if rnd.get("kind") == "practice":
            continue
        buyer = {r["rfq_id"]: r.get("buyer") for r in rnd.get("market_summary", [])}
        sent = {(o["rfq_id"], o["lot_id"]): o for o in getattr(memory, "sent", {}).get(rnd["round_id"], [])}
        for res in rnd["my_offers"]:
            o = sent.get((res["rfq_id"], res["lot_id"]))
            b = buyer.get(res["rfq_id"])
            if not o or not b:
                continue
            if res["status"] == "ABOVE_LIMIT":
                hi[b] = min(hi.get(b, 9), o["markup"])
            elif res["status"] in ("ACCEPTED", "PARTIAL"):
                lo[b] = max(lo.get(b, 0), o["markup"])
    return lo, hi


def rule_plan(state, ctx, cands):
    plan = _type_rule_plan(state, ctx, cands)
    lo, hi = buyer_bounds(ctx.memory)
    who = {c["rfq_id"]: c["buyer"] for c in cands}
    for p in plan:
        b = who.get(p["rfq_id"])
        m = p["markup"]
        if b in lo and RAISE_MAX > 0:
            m = max(m, min(lo[b] + STEP, m + RAISE_MAX))
        if b in hi:
            m = min(m, hi[b] - CAP_MARGIN)
        p["markup"] = round(max(m, MIN_MARKUP), 3)
    return plan
