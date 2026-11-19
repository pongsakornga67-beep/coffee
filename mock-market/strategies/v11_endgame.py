"""L · v4_combo ตลอดเกม + จัดการ 3 รอบสุดท้ายให้ดีขึ้น
- 3 รอบสุดท้าย: เสนอของที่เหลือทั้งหมด แบ่งโควตา 5 กระสอบตามสัดส่วนของที่เหลือแต่ละล็อต (ล็อตขายยากได้ก่อน)
  เพราะของค้างได้แค่ 0.80 × TV — ขายที่ 0.87 ยังดีกว่า
- ล็อตที่มีแต่ Instant รับ ในช่วงท้ายตั้ง 0.87–0.92 · ล็อตอื่นไม่เกิน 0.97
- ก่อนช่วงท้าย: เหมือน v4_combo ทุกอย่าง (ผลดีที่สุดในห้องทดลอง)"""
from pathlib import Path
import _base
_base.inherit(globals())

_p = Path(__file__).with_name("v4_combo.py")
exec(compile(_p.read_text(encoding="utf-8"), str(_p), "exec"), globals())
_v10 = {}
exec(compile(Path(__file__).with_name("v10_plus.py").read_text(encoding="utf-8").split("def share_capacity", 1)[1]
             .join(["def share_capacity", ""]), "v10", "exec"), globals())

ENDGAME_ROUNDS = 3
HARD_END = (0.92, 0.87)        # ล็อตขายยาก: R13 → R15
OTHER_CAP = 0.97
_combo_rule_plan = rule_plan
_combo_budget = sell_budget


def sell_budget(state, ctx):
    if rounds_left(state, ctx) <= ENDGAME_ROUNDS:
        return {l["lot_id"]: l["kg_available"] // BAG * BAG for l in state["lots"]}
    return _combo_budget(state, ctx)


def rule_plan(state, ctx, cands):
    plan = _combo_rule_plan(state, ctx, cands)
    left = rounds_left(state, ctx)
    if left > ENDGAME_ROUNDS:
        return plan
    types = {}
    for c in cands:
        types.setdefault(c["lot_id"], set()).add(c["buyer_type"])
    hard = {k for k, t in types.items() if t <= {"INSTANT"}}
    planned = {p["lot_id"] for p in plan}
    for lid in hard - planned:                      # ล็อตขายยากที่ยังไม่อยู่ในแผน: ให้ Instant RFQ ที่ใหญ่สุด
        opts = sorted([c for c in cands if c["lot_id"] == lid], key=lambda c: -c["rfq_bags"])
        lot = next(l for l in state["lots"] if l["lot_id"] == lid)
        if opts:
            plan.append({"lot_id": lid, "rfq_id": opts[0]["rfq_id"], "bags": lot["kg_available"] // BAG, "markup": 1.0})
    hi, lo = HARD_END
    hard_mk = lo + (hi - lo) * (left - 1) / max(1, ENDGAME_ROUNDS - 1)
    for p in plan:
        p["markup"] = min(p["markup"], hard_mk if p["lot_id"] in hard else OTHER_CAP)
    ctx.notes.append(f"endgame: hard lots {sorted(hard)} at x{hard_mk:.3f}")
    return share_capacity(state, plan, hard)


# ---------------------------------------------------------------- ปิดช่องโหว่ที่ตัวตรวจทุกรอบเจอ (benchmark 100 เกม)
# 1) MISSED_ROUND / โควตาเหลือ: ขอบสเปกที่เผื่อไว้ตัดล็อตชิดขอบทิ้งหมด ทั้งที่ SPEC_MISMATCH ไม่มีค่าปรับ (เสียแค่ช่องโควตา)
#    → ถ้ายังมีโควตาว่าง เสนอล็อตชิดขอบด้วย (ยกเว้นผู้ซื้อที่ล็อตนั้นเคยไม่ผ่านจริง)
# 2) BELOW_SALVAGE: TV ประเมินจากตัวอย่างอาจสูงกว่าของจริง ×0.87 ของ TV ประเมินจึงต่ำกว่า 0.80 ของ TV จริงได้
#    → ราคาขั้นต่ำต่อล็อตโตตามความไม่แน่นอนของตัวอย่าง (เมล็ดเสียมาก = คลาดมาก)

def price_floor(sample, rules=None):
    """ราคาขั้นต่ำ (×TV ประเมิน) ที่ยังได้ ≥ 0.82 × TV จริง แม้ของจริงจะแย่กว่าตัวอย่าง ~2 SD
    (เมล็ดเสียคลาดราว 0.25 + 0.05 × d · ความชื้นราว 0.2 · ล็อตชิดขอบเกรดจึงได้ขั้นต่ำสูงขึ้นเอง)"""
    if rules is None:
        return 0.88
    from appraisal import true_value as _tv
    d, m = sample["defect_pct"], sample["moisture_pct"]
    est = _tv(rules, sample)
    worst = _tv(rules, {**sample, "defect_pct": d + 2 * (0.25 + 0.05 * d), "moisture_pct": m + 0.4})
    ratio = worst / est if est else 1.0
    return round(min(0.97, max(0.86, 0.82 / ratio)), 3)


def plain_ok(sample, spec):
    return (sample["defect_pct"] <= spec["max_defect_pct"] and sample["moisture_pct"] <= spec["max_moisture_pct"]
            and (spec["min_screen"] is None or sample["screen"] >= spec["min_screen"]))


def known_fail(sample, spec):
    return any(_stricter_or_equal(spec, f) for f in FAILED.get(_fp(sample), []))


_v11_rule_plan = rule_plan
if "_guard_base" not in globals():
    _guard_base = guard


def rule_plan(state, ctx, cands):
    plan = _v11_rule_plan(state, ctx, cands)
    lots = {l["lot_id"]: l for l in state["lots"]}
    for p in plan:                                           # ราคาขั้นต่ำต่อล็อต
        p["markup"] = max(p["markup"], price_floor(lots[p["lot_id"]]["sample"], ctx.rules))
    room = state["capacity_left_kg"]
    room = (10 ** 9 if room is None else room) // BAG
    used, lot_used, rfq_used = 0, {}, {}
    for p in plan:
        used += p["bags"]
        lot_used[p["lot_id"]] = lot_used.get(p["lot_id"], 0) + p["bags"]
        rfq_used[p["rfq_id"]] = rfq_used.get(p["rfq_id"], 0) + p["bags"]
    spare = room - used
    if spare <= 0:
        return plan
    mk = markups_for(ctx.memory)
    seen = {(p["lot_id"], p["rfq_id"]) for p in plan}
    extra = []
    for r in state["rfqs"]:
        for lid, l in lots.items():
            if (lid, r["rfq_id"]) in seen or not plain_ok(l["sample"], r["spec"]) or known_fail(l["sample"], r["spec"]):
                continue
            extra.append((mk.get(r["buyer_type"], 1.0) * ctx.tv[lid], lid, r))
    extra.sort(key=lambda x: -x[0])
    for _, lid, r in extra:                                  # เติมโควตาที่ว่าง ล็อตที่ยังไม่ได้เสนอก่อน
        if spare <= 0:
            break
        free = lots[lid]["kg_available"] // BAG - lot_used.get(lid, 0)
        take = min(spare, free, r["bags"] - rfq_used.get(r["rfq_id"], 0), 2)
        if take <= 0:
            continue
        m = max(mk.get(r["buyer_type"], 1.0), price_floor(lots[lid]["sample"], ctx.rules))
        plan.append({"lot_id": lid, "rfq_id": r["rfq_id"], "bags": take, "markup": round(m, 3)})
        spare -= take
        lot_used[lid] = lot_used.get(lid, 0) + take
        rfq_used[r["rfq_id"]] = rfq_used.get(r["rfq_id"], 0) + take
    if spare < room - used:
        ctx.notes.append(f"filled spare capacity with {room - used - spare} edge bags")
    return plan


def guard(state, ctx, plan):
    """guard ของ starter ตรวจด้วย spec_ok (แบบเผื่อขอบ) จะทิ้งข้อเสนอชิดขอบที่ตั้งใจเติม → ตรวจด้วยสเปกจริงแทน"""
    global spec_ok
    strict = spec_ok
    spec_ok = lambda sample, spec: plain_ok(sample, spec) and not known_fail(sample, spec)
    try:
        return _guard_base(state, ctx, plan)
    finally:
        spec_ok = strict


if "_decide_v1" not in globals():
    _decide_v1 = decide


USE_EV_TV = True     # ตั้งราคาจาก TV ค่าคาดหวัง (เผื่อตัวอย่างคลาด) แทน TV จากสูตรตรงๆ


def decide(state, ctx):
    """ถ้าขอบสเปกตัดทุกคู่จนไม่มีข้อเสนอ ยังเติมโควตาด้วยล็อตชิดขอบได้ (ไม่พลาดรอบ)"""
    if USE_EV_TV:
        from ev_appraisal import estimate_lots_ev
        ctx.tv = {**ctx.tv, **estimate_lots_ev(ctx.rules, state["lots"])}
    offers, source = _decide_v1(state, ctx)
    if not offers and state["rfqs"]:
        offers = guard(state, ctx, rule_plan(state, ctx, []))
        source = "rules (edge fill)" if offers else source
    return offers, source
