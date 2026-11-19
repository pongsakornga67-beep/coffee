"""I · H (ราคาตามของค้าง + เรียนสะสม + เผื่อขอบสเปก) + อ่านปฏิทินแบบ C (เก็บล็อต Specialty รอเทศกาล)"""
from pathlib import Path
import _base
_base.inherit(globals())

for _name in ("v2_calendar", "v7_pressure"):
    _p = Path(__file__).with_name(_name + ".py")
    exec(compile(_p.read_text(encoding="utf-8"), str(_p), "exec"), globals())

_calendar_budget = None
_src = Path(__file__).with_name("v2_calendar.py").read_text(encoding="utf-8")
_ns = dict(globals())
exec(compile(_src, "v2_calendar", "exec"), _ns)          # sell_budget ของ C (ใช้ spec_ok ที่เผื่อขอบของ H)
_calendar_budget = _ns["sell_budget"]


def sell_budget(state, ctx):
    if rounds_left(state, ctx) <= 3:                     # 3 รอบสุดท้าย: เสนอทุกอย่างที่เหลือ
        return {l["lot_id"]: l["kg_available"] // BAG * BAG for l in state["lots"]}
    return _calendar_budget(state, ctx)
