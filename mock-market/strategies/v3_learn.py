"""D · ปรับราคาต่อ "ผู้ซื้อแต่ละราย" แบบสะสมทั้งเกม (starter ดูแค่ 3 รอบล่าสุด ต่อประเภท และรีเซ็ตกลับค่าตั้งต้นทุกรอบ)
ขายได้ครบ → ขึ้นทีละนิด · OUTBID → ลด · ABOVE_LIMIT → ลดใต้ราคาที่โดนปฏิเสธ · PARTIAL → คงไว้
แล้วเลือก RFQ ที่ได้เงินต่อ กก. มากสุดก่อน"""
import _base
_base.inherit(globals())

UP, DOWN, LIMIT_DROP = 0.015, 0.02, 0.03


def learned_markups(memory):
    """เล่นผลทุกรอบที่จำไว้ซ้ำตามลำดับ → ตัวคูณปัจจุบันของแต่ละผู้ซื้อ (key = ชื่อผู้ซื้อ) และแต่ละประเภท"""
    mk = {}
    sent_all = getattr(memory, "sent", {})
    for rnd in getattr(memory, "rounds", []):
        info = {m["rfq_id"]: (m.get("buyer") or m["buyer_type"], m["buyer_type"]) for m in rnd["market_summary"]}
        sent = {(o["rfq_id"], o["lot_id"]): o for o in sent_all.get(rnd["round_id"], [])}
        seen = {}
        for res in rnd["my_offers"]:
            o = sent.get((res["rfq_id"], res["lot_id"]))
            if not o or res["rfq_id"] not in info:
                continue
            for key in info[res["rfq_id"]]:
                seen.setdefault(key, []).append((res["status"], o["markup"]))
        for key, outs in seen.items():
            btype = key if key in BASE_MARKUP else None
            cur = mk.get(key, BASE_MARKUP.get(btype) if btype else None)
            if cur is None:
                cur = statistics_mean([x for _, x in outs])
            statuses = [s for s, _ in outs]
            if "ABOVE_LIMIT" in statuses:
                cur = min(cur, min(x for s, x in outs if s == "ABOVE_LIMIT")) - LIMIT_DROP
            elif all(s == "ACCEPTED" for s in statuses):
                cur += UP
            elif all(s == "OUTBID" for s in statuses):
                cur -= DOWN
            mk[key] = min(max(cur, MIN_MARKUP), MAX_MARKUP)
    return mk


def statistics_mean(xs):
    return sum(xs) / len(xs) if xs else 1.0


def rule_plan(state, ctx, cands):
    budget = sell_budget(state, ctx)
    rfq_left = {r["rfq_id"]: r["bags"] for r in state["rfqs"]}
    buyer = {r["rfq_id"]: r["buyer"] for r in state["rfqs"]}
    learned = learned_markups(ctx.memory)
    priced = []
    for c in cands:
        mk = learned.get(buyer[c["rfq_id"]], learned.get(c["buyer_type"], BASE_MARKUP[c["buyer_type"]]))
        priced.append((mk, c))
    plan = []
    for mk, c in sorted(priced, key=lambda x: -(x[0] * x[1]["tv"])):    # ได้เงินต่อ กก. มากสุดก่อน
        bags = min(budget[c["lot_id"]] // BAG, rfq_left[c["rfq_id"]])
        if bags > 0:
            plan.append({"lot_id": c["lot_id"], "rfq_id": c["rfq_id"], "bags": bags, "markup": round(mk, 3)})
            budget[c["lot_id"]] -= bags * BAG
            rfq_left[c["rfq_id"]] -= bags
    return plan
