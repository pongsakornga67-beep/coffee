"""H · ตั้งราคาตาม "ของค้างเทียบเวลา" + เรียนต่อประเภทแบบสะสมทั้งเกม + เผื่อขอบสเปก
- ขายช้ากว่ากำหนด (ของเหลือ % มากกว่ารอบที่เหลือ %) → ลดราคา · ขายเร็วกว่ากำหนด → ขึ้นราคา
- ตัวคูณต่อประเภทผู้ซื้อปรับสะสมทั้งเกม (ไม่รีเซ็ตทุกรอบแบบ starter)
- ช่วงท้าย (เหลือ ≤ 3 รอบ) เสนอของทั้งหมดที่เหลือ เพราะของค้างได้แค่ 0.80 × TV"""
import math
import _base
_base.inherit(globals())

DEFECT_MARGIN, MOISTURE_MARGIN = 0.6, 0.25
UP, DOWN, LIMIT_DROP = 0.015, 0.02, 0.04
PRESSURE_GAIN = 0.25                 # ตัวคูณลดลง 0.25 × (สัดส่วนที่ขายช้ากว่ากำหนด)
PACE = 1.6


def spec_ok(sample, spec):
    return (sample["defect_pct"] <= spec["max_defect_pct"] - DEFECT_MARGIN
            and sample["moisture_pct"] <= spec["max_moisture_pct"] - MOISTURE_MARGIN
            and (spec["min_screen"] is None or sample["screen"] >= spec["min_screen"]))


def markups_for(memory=None, last=3):
    mk = dict(BASE_MARKUP)
    if memory is None:
        return mk
    sent_all = getattr(memory, "sent", {})
    for rnd in getattr(memory, "rounds", []):
        if rnd.get("kind") == "practice":
            continue
        sent = {(o["rfq_id"], o["lot_id"]): o for o in sent_all.get(rnd["round_id"], [])}
        by_type = {}
        for res in rnd["my_offers"]:
            o = sent.get((res["rfq_id"], res["lot_id"]))
            if o and o.get("buyer_type") in mk:
                by_type.setdefault(o["buyer_type"], []).append((res["status"], o["markup"]))
        for t, outs in by_type.items():
            st = [s for s, _ in outs]
            if "ABOVE_LIMIT" in st:
                mk[t] = min(mk[t], min(x for s, x in outs if s == "ABOVE_LIMIT")) - LIMIT_DROP
            elif all(s == "ACCEPTED" for s in st):
                mk[t] += UP
            elif all(s == "OUTBID" for s in st):
                mk[t] -= DOWN
            mk[t] = min(max(mk[t], MIN_MARKUP), MAX_MARKUP)
    return mk


def pressure(state, ctx):
    total_r = ctx.rules["timeline"]["rounds"]
    rnd = state["round"]
    left_r = total_r if rnd["kind"] == "practice" else total_r - rnd["number"] + 1
    kg_total = sum(l["kg_total"] for l in state["lots"]) or 1
    kg_left = sum(l["kg_remaining"] for l in state["lots"])
    return kg_left / kg_total - left_r / total_r          # > 0 = ขายช้ากว่ากำหนด


def sell_budget(state, ctx):
    if rounds_left(state, ctx) <= 3:
        return {l["lot_id"]: l["kg_available"] // BAG * BAG for l in state["lots"]}
    left = rounds_left(state, ctx)
    out = {}
    for lot in state["lots"]:
        avail = lot["kg_available"] // BAG * BAG
        share = math.ceil(avail / left * PACE / BAG) * BAG
        out[lot["lot_id"]] = min(avail, max(BAG, share)) if avail >= BAG else 0
    return out


def rule_plan(state, ctx, cands):
    budget = sell_budget(state, ctx)
    adj = max(-0.06, min(0.03, -PRESSURE_GAIN * pressure(state, ctx)))
    mk = {t: min(max(v + adj, MIN_MARKUP), MAX_MARKUP) for t, v in markups_for(ctx.memory).items()}
    ctx.notes.append(f"markups {mk} (pressure adj {adj:+.3f})")
    rfq_left = {r["rfq_id"]: r["bags"] for r in state["rfqs"]}
    plan = []
    for c in sorted(cands, key=lambda c: -c["tv"]):
        bags = min(budget[c["lot_id"]] // BAG, rfq_left[c["rfq_id"]])
        if bags > 0:
            plan.append({"lot_id": c["lot_id"], "rfq_id": c["rfq_id"], "bags": bags, "markup": round(mk[c["buyer_type"]], 3)})
            budget[c["lot_id"]] -= bags * BAG
            rfq_left[c["rfq_id"]] -= bags
    return plan
