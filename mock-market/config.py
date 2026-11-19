"""ค่าตั้งของตลาดจำลอง: preset แต่ละแบบเลียนตลาดจริงคนละช่วงของงาน

  fast     ทดสอบโค้ดเร็วๆ (รอบละ 25 วิ) — ค่าเริ่มต้น
  sandbox  เหมือนตลาดฝึกวันที่ 18 (กติกาเดียวกับช่วงแข่ง แต่รอบสั้นกว่า)
  day17    เหมือนตลาดฝึก/แข่งช่วง 1 วันที่ 17 (ตลาดส่วนตัว 6 รอบ ผู้ซื้อ 2 แบบ ตัวอย่างไม่คลาด)
  contest  เหมือนช่วงแข่ง 2–3 (ตลาดรวม ซ้อม 1 + แข่ง 8 รอบ รอบละ 4 นาที ห้ามส่งด้วยมือ)

ตัวเลขที่ "ไม่เปิดเผย" ในตลาดจริง (เพดานผู้ซื้อ ความคลาดของตัวอย่าง นิสัยบอต) เป็นค่าที่เดาให้ใกล้เคียง
ปรับได้ที่ไฟล์นี้
"""

PRESETS = {
    "fast": {
        "mode": "private", "rounds": 8, "practice_rounds": 0,
        "setup_s": 3, "appraisal_s": 20, "open_s": 25, "clearing_s": 5, "restart_after_s": 20,
        "buyer_types": ["SPECIALTY", "COMMERCIAL", "INSTANT"],
        "round_capacity_factor": 1.5, "sample_noise": 1.0, "lots_per_team": 5,
        "manual_disabled": False, "reveal": True, "leaderboard_freeze_after_round": 99,
        "wait_for_start": False,
    },
    "sandbox": {
        "mode": "private", "rounds": 8, "practice_rounds": 0,
        "setup_s": 5, "appraisal_s": 120, "open_s": 60, "clearing_s": 10, "restart_after_s": 300,
        "buyer_types": ["SPECIALTY", "COMMERCIAL", "INSTANT"],
        "round_capacity_factor": 1.5, "sample_noise": 1.0, "lots_per_team": 5,
        "manual_disabled": False, "reveal": True, "leaderboard_freeze_after_round": 99,
        "wait_for_start": False,
    },
    "day17": {
        "mode": "private", "rounds": 6, "practice_rounds": 0,
        "setup_s": 5, "appraisal_s": 600, "open_s": 180, "clearing_s": 10, "restart_after_s": 300,
        "buyer_types": ["COMMERCIAL", "INSTANT"],
        "round_capacity_factor": None, "sample_noise": 0.0, "lots_per_team": 4,
        "manual_disabled": False, "reveal": True, "leaderboard_freeze_after_round": 99,
        "wait_for_start": False,
    },
    # ด่าน 3 วันที่ 20 (สไลด์ "กติกาวันที่ 20"): SETUP 10 นาที · ประเมิน 15 นาที · ซ้อม 2 + จริง 15 รอบ
    # รอบละ 6 นาที (เปิดรับ 5 + จับคู่ 1) · 45 กระสอบ 5 ล็อต · อันดับหยุดหลัง R12 · บอต 100 ทีม
    "stage3": {
        "standard_lots": True,    # ล็อตมาตรฐานเหมือนกันทุกทีม ผ่านสเปกทุกผู้ซื้อ · สุ่มใหม่ตอนกด Start
        "mode": "shared", "rounds": 15, "practice_rounds": 2,
        "setup_s": 600, "appraisal_s": 900, "open_s": 300, "clearing_s": 60, "restart_after_s": None,
        "buyer_types": ["SPECIALTY", "COMMERCIAL", "INSTANT"],
        "round_capacity_factor": 1.5, "sample_noise": 1.0, "lots_per_team": 5, "total_bags": 45,
        "manual_disabled": False, "reveal": True, "leaderboard_freeze_after_round": 99,
        "wait_for_start": True, "bots": 100,
        "calendar": [(1, 5, "ปกติ", 1.0, None), (6, 8, "เทศกาลกาแฟ", 1.4, 0.40), (9, 12, "ปกติ", 1.0, None),
                     (13, 14, "ใกล้ปิดตลาด", 0.7, None), (15, 15, "รอบสุดท้าย", 0.45, None)],
    },
    "contest": {
        "mode": "shared", "rounds": 8, "practice_rounds": 1,
        "setup_s": 5, "appraisal_s": 600, "open_s": 240, "clearing_s": 10, "restart_after_s": None,
        "buyer_types": ["SPECIALTY", "COMMERCIAL", "INSTANT"],
        "round_capacity_factor": 1.5, "sample_noise": 1.0, "lots_per_team": 5,
        "manual_disabled": True, "reveal": False, "leaderboard_freeze_after_round": 6,
        "wait_for_start": True,
    },
}

# ล็อตแบบสุ่มยากเหมือนเดิม (ชิดขอบสเปก มีค่าเสีย/ความชื้นสูง ผลวัดคลาด) ไว้ทดสอบกลยุทธ์ให้ทนทาน
PRESETS["stage3_hard"] = {**PRESETS["stage3"], "standard_lots": False}

# ผู้ซื้อ: ชื่อตามหน้าเว็บจริง · ceiling = เพดานลับ (ราคา ÷ TV จริง) · spec = สเปกขั้นต่ำ
# ตลาดจริงไม่บอกเพดาน ค่าข้างล่างเดาจากผลตลาดฝึก (Specialty จ่ายสูงสุด Instant ต่ำสุด) และสุ่มขยับทุกเกม
BUYERS = [
    {"buyer_id": "B1", "buyer": "1 ดอยช้างโรสเตอร์", "buyer_type": "SPECIALTY", "ceiling": 1.22,
     "spec": {"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 16}},
    {"buyer_id": "B2", "buyer": "2 บางกอกบลูม", "buyer_type": "SPECIALTY", "ceiling": 1.16,
     "spec": {"max_defect_pct": 4.0, "max_moisture_pct": 13.0, "min_screen": 15}},
    {"buyer_id": "B3", "buyer": "3 สยามคอมเมิร์ซ", "buyer_type": "COMMERCIAL", "ceiling": 1.06,
     "spec": {"max_defect_pct": 7.0, "max_moisture_pct": 13.5, "min_screen": None}},
    {"buyer_id": "B4", "buyer": "4 ชุมพรคั่วบด", "buyer_type": "COMMERCIAL", "ceiling": 1.10,
     "spec": {"max_defect_pct": 5.0, "max_moisture_pct": 13.0, "min_screen": 14}},
    {"buyer_id": "B5", "buyer": "5 อินสแตนท์ไทย", "buyer_type": "INSTANT", "ceiling": 0.97,
     "spec": {"max_defect_pct": 15.0, "max_moisture_pct": 15.0, "min_screen": None}},
    {"buyer_id": "B6", "buyer": "6 ทรีอินวันฟู้ดส์", "buyer_type": "INSTANT", "ceiling": 1.01,
     "spec": {"max_defect_pct": 10.0, "max_moisture_pct": 14.5, "min_screen": 13}},
]

# ความต้องการรวมต่อรอบ = กระสอบที่ทุกทีมมี ÷ จำนวนรอบ × น้ำหนักปฏิทิน × สุ่มในช่วงนี้
# (> 1 เล็กน้อย เพราะผู้ซื้อแต่ละรายรับได้แค่บางเกรด: ถ้าเท่ากับ 1 ของเกรดต่ำจะค้างทั้งตลาด)
DEMAND_FACTOR = (1.0, 1.15)

# ส่วนแบ่งความต้องการปกติของแต่ละประเภท (ปฏิทินรอบเทศกาลเพิ่ม Specialty)
TYPE_SHARE = {"SPECIALTY": 0.15, "COMMERCIAL": 0.47, "INSTANT": 0.38}

# สูตรมูลค่า: ตรงกับสไลด์วันที่ 17 และ GET /v1/calendar ของตลาดจริง
RULES_BASE = {
    "p0": 120.0,
    "defect_steps": [
        {"max_defect_pct": 1.0, "grade": "PREMIUM", "step": 1.08},
        {"max_defect_pct": 3.0, "grade": "GRADE_1", "step": 1.04},
        {"max_defect_pct": 5.0, "grade": "GRADE_2", "step": 1.00},
        {"max_defect_pct": 7.0, "grade": "GRADE_3", "step": 0.96},
        {"max_defect_pct": 15.0, "grade": "BELOW_STANDARD", "step": 0.85},
        {"max_defect_pct": 100.0, "grade": "REJECT", "step": 0.60},
    ],
    "moisture": {"ref": 12.5, "hard_limit": 15.0, "penalty_per_pp": 0.02, "hard_factor": 0.60},
    "screen_factors": {"12": 0.92, "13": 0.97, "14": 1.00, "15": 1.00, "16": 1.03, "17": 1.03, "18": 1.06},
    "true_value_formula": "TV = P0 × G × M × S",
    "G": "G = (1 − d/100) × step(d)",
    "M": "m ≤ ref → 1 · ref < m ≤ hard_limit → (1 − m/100) ÷ (1 − ref/100) − penalty_per_pp × (m − ref) · m > hard_limit → hard_factor",
    "bag_kg": 60,
    "salvage_ratio": 0.8,
}

ORIGINS = [("ชุมพร - ท่าแซะ", "Natural (Dry)"), ("ชุมพร - หลังสวน", "Honey"), ("ชุมพร - ปะทิว", "Washed"),
           ("สุราษฎร์ธานี - ท่าชนะ", "Natural (Dry)"), ("ระนอง - กระบุรี", "Washed"),
           ("ชุมพร - สวี", "Natural (Dry)"), ("กระบี่ - เขาพนม", "Honey")]

BOT_NAMES = ["ไร่เขาทะลุ", "สวนลุงชาญ", "ไร่ปากน้ำ", "ไร่ท่าแซะ", "สหกรณ์หลังสวน", "ไร่พะโต๊ะ", "ไร่สวี",
             "ไร่ละแม", "ไร่ทุ่งตะโก", "บ้านนาสาร", "ไร่คลองวัง", "ไร่ห้วยแก้ว", "ไร่เขาค่าย", "ไร่ดอนยาง",
             "ไร่วังไผ่", "ไร่บางสน", "ไร่นาคราช", "ไร่ปังหวาน", "ไร่ทรัพย์อนันต์", "ไร่เขาพลู", "ไร่บ่อคำ",
             "ไร่ตะโก", "ไร่ถ้ำสิงห์", "ไร่วิสัย"]
