"""C · อ่านปฏิทิน: ขายมากในรอบที่ความต้องการสูง เก็บล็อตเกรด Specialty ไว้ขายช่วงเทศกาล
และขายให้หมดก่อนช่วงท้าย (R13–R15 ความต้องการต่ำ ของค้างเหลือแค่ 80%)"""
import math
import _base
_base.inherit(globals())

HOLD_FOR_FESTIVAL = 0.35   # รอบปกติก่อนเทศกาล: ล็อตที่เข้าสเปก Specialty ขายแค่สัดส่วนนี้ของปกติ
FINISH_BY_WEIGHT = 0.6     # กระจายของให้หมดภายในรอบที่ความต้องการ ≥ ค่านี้ (ข้ามรอบท้ายที่ต้องการน้อย)

SPECIALTY_SPECS = []       # สเปกของผู้ซื้อ Specialty ที่เคยเห็น (จำข้ามรอบ)


def sell_budget(state, ctx):
    rnd, cal = state["round"], _base.calendar_of(ctx)
    for r in state["rfqs"]:
        if r["buyer_type"] == "SPECIALTY" and r["spec"] not in SPECIALTY_SPECS:
            SPECIALTY_SPECS.append(r["spec"])
    total = ctx.rules["timeline"]["rounds"]
    if rnd["kind"] != "real":
        now_n, ahead = 1, [cal[n] for n in range(1, total + 1) if n in cal]
    else:
        now_n, ahead = rnd["number"], [cal[n] for n in range(rnd["number"], total + 1) if n in cal]
    weights = [c.get("demand_weight") or 1 for c in ahead] or [1]
    good = [w for w in weights if w >= FINISH_BY_WEIGHT] or weights
    share_now = weights[0] / sum(good) if weights[0] >= FINISH_BY_WEIGHT else weights[0] / sum(weights)
    fest_ahead = any("เทศกาล" in c["label"] or "festival" in c["label"].lower() for c in ahead[1:])
    is_fest = "เทศกาล" in ahead[0]["label"]
    out = {}
    for lot in state["lots"]:
        avail = lot["kg_available"] // BAG * BAG
        if avail < BAG:
            out[lot["lot_id"]] = 0
            continue
        w = 1.0
        if fest_ahead and not is_fest and any(spec_ok(lot["sample"], s) for s in SPECIALTY_SPECS):
            w = HOLD_FOR_FESTIVAL
        share = avail if len(good) <= 1 and weights[0] >= FINISH_BY_WEIGHT else math.ceil(avail * share_now * PACE * w / BAG) * BAG
        out[lot["lot_id"]] = min(avail, max(BAG, share))
    return out
