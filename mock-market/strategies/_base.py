"""ให้ไฟล์กลยุทธ์ "สืบทอด" strategy.py ของทีม แล้วเขียนทับเฉพาะฟังก์ชันที่อยากลอง

    import _base; _base.inherit(globals())     # ได้ทุกอย่างจาก strategy.py ของทีม
    def spec_ok(sample, spec): ...             # เขียนทับเฉพาะจุดที่เปลี่ยน

ฟังก์ชันเดิมใน strategy.py จะเรียกตัวที่เขียนทับเอง (เพราะอยู่ใน globals เดียวกัน)
"""
import os
from pathlib import Path

TEAM_STRATEGY = Path(os.environ.get("TEAM_STRATEGY", Path(__file__).resolve().parents[2] / "strategy.py"))


def inherit(g):
    if g.get("_INHERITED"):          # รวมหลายไฟล์ (v4_combo) ไม่ต้องโหลดซ้ำ
        return
    g["_INHERITED"] = True
    src = TEAM_STRATEGY.read_text(encoding="utf-8")
    exec(compile(src, str(TEAM_STRATEGY), "exec"), g)
    # strategy.py รุ่นแรกของ starter มีแค่ MARKUP (1.00 ทุกประเภท) ไม่มี BASE_MARKUP/markups_for → เติมให้ใช้ร่วมกันได้
    if "BASE_MARKUP" not in g:
        g["BASE_MARKUP"] = g["MARKUP"]
    if "markups_for" not in g:
        g["markups_for"] = lambda memory=None, last=3: dict(g["BASE_MARKUP"])


def calendar_of(ctx):
    """ปฏิทินเป็น dict: เลขรอบ -> {label, demand_weight, specialty_share}"""
    return {c.get("round", (c.get("rounds") or [0])[0]): c for c in ctx.calendar}
