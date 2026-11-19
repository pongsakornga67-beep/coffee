"""K · v4_combo + จัดการ "ล็อตขายยาก" และช่วงท้าย
จาก log/ห้องทดลอง: ของค้างส่วนใหญ่คือล็อตเกรดต่ำ (BELOW_STANDARD) ที่มีแต่ผู้ซื้อ Instant รับ
และทุกทีมมีล็อตแบบเดียวกัน → แข่งราคากันที่ Instant · ขายที่ 0.87 × TV ยังดีกว่าค้างได้ 0.80
- ล็อตที่ขายได้แค่ Instant: ราคาเริ่ม ~0.95 แล้วลดลงตามเวลาจนถึง 0.87 · เสนอทุกรอบ
- 3 รอบสุดท้าย: เสนอทุกอย่างที่เหลือ และไม่ตั้งเกิน 0.95 × TV"""
from pathlib import Path
import _base
_base.inherit(globals())

_p = Path(__file__).with_name("v4_combo.py")
exec(compile(_p.read_text(encoding="utf-8"), str(_p), "exec"), globals())

HARD_START, HARD_FLOOR = 0.95, 0.87
ENDGAME_ROUNDS, ENDGAME_CAP = 3, 0.95
_combo_rule_plan = rule_plan
_combo_budget = sell_budget


def _hard_lots(state, cands):
    types = {}
    for c in cands:
        types.setdefault(c["lot_id"], set()).add(c["buyer_type"])
    return {lid for lid, t in types.items() if t <= {"INSTANT"}}


def sell_budget(state, ctx):
    if rounds_left(state, ctx) <= ENDGAME_ROUNDS:
        return {l["lot_id"]: l["kg_available"] // BAG * BAG for l in state["lots"]}
    return _combo_budget(state, ctx)


def rule_plan(state, ctx, cands):
    plan = _combo_rule_plan(state, ctx, cands)
    total = ctx.rules["timeline"]["rounds"]
    left = rounds_left(state, ctx)
    hard = _hard_lots(state, cands)
    hard_mk = HARD_FLOOR + (HARD_START - HARD_FLOOR) * max(0, left - 1) / max(1, total - 1)
    planned = {p["lot_id"] for p in plan}
    # ล็อตขายยากที่ยังไม่ได้อยู่ในแผน: ใส่ให้ Instant RFQ ที่ใหญ่สุด (มีโควตาเหลือหรือไม่ guard จะตัดเอง)
    for lid in hard - planned:
        opts = sorted([c for c in cands if c["lot_id"] == lid], key=lambda c: -c["rfq_bags"])
        lot = next(l for l in state["lots"] if l["lot_id"] == lid)
        bags = max(1, min(lot["kg_available"] // BAG, max(1, (lot["kg_available"] // BAG) // max(1, left) + 1)))
        if opts:
            plan.insert(0, {"lot_id": lid, "rfq_id": opts[0]["rfq_id"], "bags": bags, "markup": hard_mk})
    for p in plan:
        if p["lot_id"] in hard:
            p["markup"] = min(p["markup"], hard_mk)
        if left <= ENDGAME_ROUNDS:
            p["markup"] = min(p["markup"], ENDGAME_CAP)
    return share_capacity(state, plan, hard)


def share_capacity(state, plan, hard):
    """แบ่งโควตากระสอบของรอบตามสัดส่วนของที่เหลือแต่ละล็อต (ล็อตขายยากได้ก่อน)
    ไม่ให้ล็อตมูลค่าสูงกินโควตาหมดจนล็อตเกรดต่ำไม่เคยได้เสนอ"""
    room = state["capacity_left_kg"]
    if room is None or not plan:
        return plan
    room //= BAG
    want = {}
    for p in plan:
        want[p["lot_id"]] = want.get(p["lot_id"], 0) + p["bags"]
    left_bags = {l["lot_id"]: l["kg_available"] // BAG for l in state["lots"] if l["lot_id"] in want}
    total = sum(min(want[k], left_bags[k]) for k in want) or 1
    if total <= room:
        return sorted(plan, key=lambda p: p["lot_id"] not in hard)
    quota = {k: (min(want[k], left_bags[k]) * room) // total for k in want}
    order = sorted(want, key=lambda k: (k not in hard, -left_bags[k]))
    spare = room - sum(quota.values())
    while spare > 0:
        moved = False
        for k in order:
            if spare > 0 and quota[k] < min(want[k], left_bags[k]):
                quota[k] += 1
                spare -= 1
                moved = True
        if not moved:
            break
    out = []
    for p in sorted(plan, key=lambda p: p["lot_id"] not in hard):
        take = min(p["bags"], quota[p["lot_id"]])
        if take > 0:
            out.append({**p, "bags": take})
            quota[p["lot_id"]] -= take
    return out
