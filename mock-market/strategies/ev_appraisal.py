"""ประเมิน TV แบบ "ค่าคาดหวัง" (expected value): เผื่อว่าตัวอย่าง 350 ก. คลาดจากของจริง

สูตร TV กระโดดที่ขอบเกรด (เช่น เมล็ดเสีย 6.99% = GRADE_3 ×0.96 แต่ 7.01% = BELOW_STANDARD ×0.85)
ล็อตที่ตัวอย่างอยู่ชิดขอบ TV จริงอาจต่างจากที่คิดตรงๆ ถึง 10%+ → เฉลี่ย TV ข้ามความคลาดที่เป็นไปได้แทน
ความคลาด (SD) ตั้งต้น: เมล็ดเสีย 0.25 + 0.05 × d · ความชื้น 0.2 · ปรับได้จากผล reveal ของตลาดฝึก

    from ev_appraisal import estimate_lots_ev
    tv = estimate_lots_ev(rules, lots)
"""
import math

DEFECT_SD = (0.25, 0.05)     # SD = a + b × d
MOISTURE_SD = 0.20
# จุด Gauss–Hermite 7 จุด (ค่าเฉลี่ยของฟังก์ชันใต้การแจกแจงปกติ)
_GH = [(-2.651961356835233, 0.0009717812450995), (-1.673551628767471, 0.05451558281912703),
       (-0.8162878828589647, 0.4256072526101278), (0.0, 0.8102646175568073),
       (0.8162878828589647, 0.4256072526101278), (1.673551628767471, 0.05451558281912703),
       (2.651961356835233, 0.0009717812450995)]
_NORM = math.sqrt(math.pi)


def _tv(rules, d, m, s):
    from appraisal import true_value
    return true_value(rules, {"defect_pct": max(0.0, d), "moisture_pct": m, "screen": s})


def ev_true_value(rules, sample):
    d, m, s = sample["defect_pct"], sample["moisture_pct"], sample["screen"]
    sd_d = DEFECT_SD[0] + DEFECT_SD[1] * d
    tot = 0.0
    for xd, wd in _GH:
        for xm, wm in _GH:
            tot += wd * wm * _tv(rules, d + math.sqrt(2) * sd_d * xd, m + math.sqrt(2) * MOISTURE_SD * xm, s)
    return tot / (_NORM * _NORM)


def estimate_lots_ev(rules, lots):
    return {l["lot_id"]: round(ev_true_value(rules, l["sample"]), 2) for l in lots}
