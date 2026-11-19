"""F · starter แต่ตั้งราคาสูงขึ้น 5% ทุกประเภท (ทดสอบว่าแพงขึ้นคุ้มไหม)"""
import _base
_base.inherit(globals())
BASE_MARKUP = {k: v + 0.05 for k, v in BASE_MARKUP.items()}
MARKUP = BASE_MARKUP          # starter รุ่นแรกใช้ MARKUP ตรงๆ
