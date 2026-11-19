"""G · starter แต่ตั้งราคาต่ำลง 4% และขายเร็วขึ้น (PACE 1.8) (ทดสอบว่าขายหมดเร็วคุ้มไหม)"""
import _base
_base.inherit(globals())
BASE_MARKUP = {k: v - 0.04 for k, v in BASE_MARKUP.items()}
MARKUP = BASE_MARKUP          # starter รุ่นแรกใช้ MARKUP ตรงๆ
PACE = 1.8
