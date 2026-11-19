"""E · รวม B + C + D: เผื่อขอบสเปก + อ่านปฏิทิน + เรียนเพดานผู้ซื้อแต่ละราย"""
from pathlib import Path
import _base
_base.inherit(globals())

for _name in ("v1_margin", "v2_calendar", "v3_learn"):      # เอาโค้ดทั้งสามไฟล์มาซ้อนกันในโมดูลนี้
    _p = Path(__file__).with_name(_name + ".py")
    exec(compile(_p.read_text(encoding="utf-8"), str(_p), "exec"), globals())

# กฎล้วน: ห้องทดลองวัดผลแบบไม่ใช้ LLM · LLM ตัวเล็กตั้งราคาเองแล้วแพงไป (OUTBID/ABOVE_LIMIT) และช้า
# เปลี่ยนเป็น True ถ้าอยากให้ LLM ช่วยเลือก (กฎยังเติมส่วนที่เหลือเหมือน starter)
USE_LLM = False
_llm_plan_base = globals().get("_llm_plan_base") or llm_plan


def llm_plan(state, ctx, cands):
    if not USE_LLM:
        ctx.notes.append("rules only (USE_LLM = False)")
        return None
    return _llm_plan_base(state, ctx, cands)
