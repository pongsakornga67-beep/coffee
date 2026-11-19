"""B · เผื่อ noise ของผลวัดตัวอย่าง + จำล็อตที่เคยโดน SPEC_MISMATCH (กันโดนซ้ำ)
- ขอบที่เผื่อโตตาม % เมล็ดเสีย (ตัวอย่าง 350 ก. คลาดมากขึ้นเมื่อเมล็ดเสียเยอะ)
- ล็อตที่เคยโดน SPEC_MISMATCH กับสเปกไหน → ไม่เสนอให้สเปกที่เข้มเท่าหรือเข้มกว่านั้นอีก"""
import _base
_base.inherit(globals())

DEFECT_MARGIN = 0.4          # ขอบพื้นฐาน (% เมล็ดเสีย) + 0.08 × % เมล็ดเสียของตัวอย่าง
DEFECT_MARGIN_PER_PCT = 0.08
MOISTURE_MARGIN = 0.3
SPEC_SEEN = {}               # rfq_id -> spec (จำจากทุกรอบที่เห็น)
BUYER_SPEC = {}              # ผู้ซื้อ -> spec ล่าสุดที่เห็น
FAILED = {}                  # ลายนิ้วมือของล็อต -> [สเปกที่เคยไม่ผ่านของจริง]


def _fp(sample):
    return (sample["defect_pct"], sample["moisture_pct"], sample["screen"])


def _stricter_or_equal(spec, failed):
    return (spec["max_defect_pct"] <= failed["max_defect_pct"] and spec["max_moisture_pct"] <= failed["max_moisture_pct"]
            and (spec["min_screen"] or 0) >= (failed["min_screen"] or 0))


def spec_ok(sample, spec):
    d = sample["defect_pct"]
    if any(_stricter_or_equal(spec, f) for f in FAILED.get(_fp(sample), [])):
        return False
    return (d <= spec["max_defect_pct"] - (DEFECT_MARGIN + DEFECT_MARGIN_PER_PCT * d)
            and sample["moisture_pct"] <= spec["max_moisture_pct"] - MOISTURE_MARGIN
            and (spec["min_screen"] is None or sample["screen"] >= spec["min_screen"]))


def learn_spec_mismatch(state, memory):
    """อ่านผลทุกรอบ: ล็อตไหนโดน SPEC_MISMATCH กับผู้ซื้อรายไหน → จำสเปกของผู้ซื้อรายนั้น
    (จำตามผู้ซื้อ ไม่ใช่ rfq_id: agent ที่รีสตาร์ทกลางเกมก็ยังรู้ เพราะผลรอบบอกชื่อผู้ซื้อเสมอ)"""
    for r in state["rfqs"]:
        SPEC_SEEN[r["rfq_id"]] = r["spec"]
        BUYER_SPEC[r.get("buyer_id") or r["buyer"]] = r["spec"]
        BUYER_SPEC[r["buyer"]] = r["spec"]
    samples = {l["lot_id"]: l["sample"] for l in state["lots"]}
    for rnd in getattr(memory, "rounds", []):
        who = {m["rfq_id"]: (m.get("buyer_id"), m.get("buyer")) for m in rnd.get("market_summary", [])}
        for o in rnd["my_offers"]:
            if o["status"] != "SPEC_MISMATCH" or o["lot_id"] not in samples:
                continue
            bid, bname = who.get(o["rfq_id"], (None, None))
            spec = SPEC_SEEN.get(o["rfq_id"]) or BUYER_SPEC.get(bid) or BUYER_SPEC.get(bname)
            if spec is None:
                continue
            lst = FAILED.setdefault(_fp(samples[o["lot_id"]]), [])
            if spec not in lst:
                lst.append(spec)


if "_decide_base" not in globals():          # ไฟล์นี้อาจถูกรวมซ้ำ (v4/v10) อย่าห่อซ้อนจนเรียกตัวเอง
    _decide_base = decide


def decide(state, ctx):
    learn_spec_mismatch(state, ctx.memory)
    return _decide_base(state, ctx)
