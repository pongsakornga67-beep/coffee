"""M · v11_endgame + อ่านตลาดทุกรอบแล้วปรับราคาตามคู่แข่ง (ไม่ใช่แค่ผลของตัวเอง)
v3 ปรับตามผลข้อเสนอของเราเอง (ขยับทีละ 1.5–2%) · ชั้นนี้เพิ่มการอ่าน "ราคาที่ตลาดเคลียร์จริง" ของรอบล่าสุดต่อผู้ซื้อแต่ละราย:
- OUTBID: ดูราคาต่ำสุดที่ผู้ชนะขายได้ (price_min) ถ้าเราแพงกว่ามาก ลดเพิ่มให้ใกล้ราคานั้น (สูงสุดอีก 3%)
- (ปิดอยู่) ACCEPTED เต็ม แต่คู่แข่งขายได้สูงกว่า → ขึ้นเพิ่ม: ทดลองแล้วทำให้ PR ตก
ราคาต่อ กก. ของต่างล็อตเทียบกันได้หยาบ (TV ต่างกัน) จึงจำกัดขนาดการปรับและขยับแค่ครึ่งหนึ่งของช่องว่าง"""
from pathlib import Path
import _base
_base.inherit(globals())
exec(compile(Path(__file__).with_name("v11_endgame.py").read_text(encoding="utf-8"), "v11", "exec"), globals())

MARKET_STEP_CAP = 0.03
MARKET_ENABLED = True
ALLOW_RAISE = False      # ทดลองแล้ว: ขึ้นราคาตามคู่แข่งทำให้ ABOVE_LIMIT เพิ่มและ PR ตก (ราคาต่อ กก. ต่างล็อตเทียบกันตรงๆ ไม่ได้)
_learned_v3 = learned_markups


def learned_markups(memory):
    mk = _learned_v3(memory)
    rounds = getattr(memory, "rounds", [])
    if not MARKET_ENABLED or not rounds:
        return mk
    last = rounds[-1]
    summ = {m["rfq_id"]: m for m in last["market_summary"]}
    for res in last["my_offers"]:
        m = summ.get(res["rfq_id"])
        if not m or not res.get("price_per_kg"):
            continue
        key = m.get("buyer") or m["buyer_type"]
        if key not in mk:
            continue
        price = res["price_per_kg"]
        adj = 0.0
        if res["status"] == "OUTBID" and m.get("price_min"):
            gap = m["price_min"] / price - 1                    # ติดลบ = เราแพงกว่าผู้ชนะรายแพงสุดที่ยังได้
            if gap < -0.02:                                      # v3 ลดไปแล้ว 2% ส่วนที่เกินนั้นเราลดเพิ่มครึ่งหนึ่ง
                adj = max(-MARKET_STEP_CAP, 0.5 * (gap + 0.02))
        elif ALLOW_RAISE and res["status"] == "ACCEPTED" and m.get("price_max") and m["price_max"] > price * 1.03:
            adj = min(MARKET_STEP_CAP, 0.4 * (m["price_max"] / price - 1))
        if adj:
            mk[key] = min(max(mk[key] + adj, MIN_MARKUP), MAX_MARKUP)
    return mk
