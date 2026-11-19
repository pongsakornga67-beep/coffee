"""v14_best = v8_best + ไม่ขึ้นราคาเอง (UP=0)
ที่มา: สวีปบนล็อตมาตรฐาน (stage3) 120 เกม ×2 ชุด seed — PR 0.9614 → 0.967 (ค้าง 9.8% → 1.5%, ABOVE_LIMIT 11% → 5.8%)
เหตุผล: v8 เดิมขึ้นราคาเมื่อขายหมดเร็ว แล้วชนเพดานผู้ซื้อ (ABOVE_LIMIT) จึงลงโทษตัวเอง; ตอนนี้ลดอย่างเดียว ไม่ไล่ขึ้น
หมายเหตุ: บนล็อตแบบสุ่มเก่า (stage3_hard) แย่กว่า v8 เล็กน้อย (0.932 vs 0.938)
"""
from pathlib import Path
import _base
_base.inherit(globals())
exec(compile(Path(__file__).with_name("v8_best.py").read_text(encoding="utf-8"), "v8", "exec"), globals())
UP = 0.0
