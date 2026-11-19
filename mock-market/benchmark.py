"""ห้องทดลองกลยุทธ์: เล่นด่าน 3 จำลอง (15 รอบ + ซ้อม 2, บอต 100 ทีม) หลายเกม แล้วเทียบ PR ของแต่ละกลยุทธ์

    python mock-market/benchmark.py                      # ทุกกลยุทธ์ใน strategies/ · 30 เกม
    python mock-market/benchmark.py --games 60 v0_starter v4_combo
    python mock-market/benchmark.py --games 10 --strategy-file strategy.py   # เทียบ strategy.py ที่ทีมกำลังแก้

ไม่ใช้เวลาจริง ไม่ใช้ HTTP ไม่ใช้ LLM (กฎล้วน) · ทุกกลยุทธ์เจอโลกเดียวกัน (seed เดียวกัน) จึงเทียบกันได้ยุติธรรม
ผลขึ้นกับค่าที่ "เดา" ใน config.py (เพดานผู้ซื้อ ความคลาดตัวอย่าง นิสัยบอต) → ดูว่าแบบไหน "ดีกว่า" มากกว่าตัวเลข PR
"""
import os
os.environ.setdefault('MOCK_NO_LOG','1')
import argparse
import importlib.util
import json
import os
import random
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
STARTER = HERE.parent
sys.path[:0] = [str(HERE), str(HERE / "strategies"), str(STARTER)]

from config import PRESETS                       # noqa: E402
from engine import BAG, Game                     # noqa: E402
from server import state_view                    # noqa: E402

import appraisal                                 # noqa: E402  (appraisal.py ของทีม)
from memory import Memory                        # noqa: E402  (memory.py ของทีม)


def load_strategy(path, tag):
    spec = importlib.util.spec_from_file_location(f"strat_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EV_APPRAISAL = False


def play(path, seed, cfg, tmp):
    mod = load_strategy(path, f"{Path(path).stem}_{seed}")     # โหลดใหม่ทุกเกม (บางกลยุทธ์จำค่าในตัวแปร)
    g = Game(cfg, seed=seed, name="bench")
    me = g.seller("T01")
    now = time.time()
    g.start(now)
    rules = g.rules
    mem = Memory(Path(tmp) / f"mem-{Path(path).stem}-{seed}.json")
    rng = random.Random(seed)
    tv = None
    while g.phase != "CLOSED":
        st = state_view(g, me, now)
        if g.phase == "APPRAISAL" and me.appraisal is None:
            tv = appraisal.estimate_lots(rules, st["lots"])
            if EV_APPRAISAL:
                from ev_appraisal import estimate_lots_ev
                tv = estimate_lots_ev(rules, st["lots"])
            items = appraisal.appraisal_items(rules, st["lots"], tv)
            me.appraisal = {"submitted_at": now, "items": items, "by_lot": {i["lot_id"]: i["est_value_per_kg"] for i in items}}
        elif g.phase == "ROUND_OPEN":
            mem.record_results(st["last_results"])
            ctx = mod.Context(rules, g.calendar, tv, rng, mem, None, 999)
            offers, _ = mod.decide(st, ctx)
            errors, ok = g.place(me, offers, True)
            if errors:
                raise RuntimeError(f"{path}: market rejected offers: {errors[:2]}")
            g.commit(me, ok, True)
            mem.record_sent(st["round"]["round_id"], offers, st, tv)
        g.skip(now, force=True)
    status = Counter()
    sold_markup = []
    for rid, res in me.results.items():
        if res["kind"] != "real":
            continue
        for o in res["my_offers"]:
            status[o["status"]] += 1
            if o["filled_kg"]:
                lot = me.lots[o["lot_id"]]
                sold_markup += [o["price_per_kg"] / lot["tv"]] * (o["filled_kg"] // BAG)
    best_bot = max(s.score["pr"] for s in g.sellers.values() if s.is_bot)
    rank = 1 + sum(1 for s in g.sellers.values() if s.score["total"] > me.score["total"])
    return {"pr": me.score["pr"], "total": me.score["total"], "mape": me.score["mape"], "rank": rank,
            "unsold": me.kg_remaining() / me.kg_total(), "status": status, "best_bot": best_bot,
            "sold_markup": statistics.mean(sold_markup) if sold_markup else None,
            "audit": Counter(a["code"] for a in me.audit if a["round"].startswith("R")),
            "critical": [f"seed {seed} {a['round']} {a['code']}: {a['msg']}" for a in me.audit if a["level"] == "critical"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="ชื่อไฟล์ใน strategies/ (ไม่ใส่ = ทุกไฟล์)")
    ap.add_argument("--games", type=int, default=30)
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--preset", default="stage3")
    ap.add_argument("--bots", type=int)
    ap.add_argument("--strategy-file", action="append", default=[], help="ไฟล์กลยุทธ์อื่น เช่น strategy.py")
    ap.add_argument("--json", help="บันทึกผลเป็น json")
    ap.add_argument("--ev-appraisal", action="store_true", help="ส่งผลประเมินแบบค่าคาดหวัง (strategies/ev_appraisal.py)")
    a = ap.parse_args()
    global EV_APPRAISAL
    EV_APPRAISAL = a.ev_appraisal
    cfg = dict(PRESETS[a.preset])
    if a.bots is not None:
        cfg["bots"] = a.bots
    files = [HERE / "strategies" / f"{n}.py" for n in a.names] or sorted((HERE / "strategies").glob("v*.py"))
    files += [Path(f).resolve() for f in a.strategy_file]
    seeds = [a.seed + i for i in range(a.games)]
    print(f"preset {a.preset} · บอต {cfg.get('bots')} · {a.games} เกมต่อกลยุทธ์ · กฎล้วน (ไม่ใช้ LLM)\n")
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            t0 = time.time()
            res = [play(f, s, cfg, tmp) for s in seeds]
            out[f.stem] = res
            print(f"  {f.stem:<14} เสร็จใน {time.time() - t0:.0f} วิ", flush=True)
    base = out.get("v0_starter")
    print(f"\n{'กลยุทธ์':<14} {'PR เฉลี่ย':>9} {'±SE':>6} {'ชนะ A':>6} {'คะแนน':>6} {'อันดับ':>6} {'ค้าง%':>6} {'×TV ขาย':>8}"
          f" {'SPEC_MIS':>8} {'ABOVE':>6} {'OUTBID':>6}")
    for name, res in out.items():
        prs = [r["pr"] for r in res]
        se = statistics.stdev(prs) / len(prs) ** 0.5 if len(prs) > 1 else 0
        win = f"{sum(r['pr'] > b['pr'] for r, b in zip(res, base)) / len(res):.0%}" if base and name != "v0_starter" else "–"
        st = sum((r["status"] for r in res), Counter())
        n = sum(st.values()) or 1
        sm = [r["sold_markup"] for r in res if r["sold_markup"]]
        print(f"{name:<14} {statistics.mean(prs):>9.4f} {se:>6.4f} {win:>6} {statistics.mean(r['total'] for r in res):>6.1f}"
              f" {statistics.mean(r['rank'] for r in res):>6.1f} {statistics.mean(r['unsold'] for r in res):>6.1%}"
              f" {statistics.mean(sm):>8.3f} {st['SPEC_MISMATCH'] / n:>8.1%} {st['ABOVE_LIMIT'] / n:>6.1%} {st['OUTBID'] / n:>6.1%}")
    print("\nประเมินคลาดเฉลี่ย (MAPE): " + " · ".join(f"{k} {statistics.mean(r['mape'] for r in v):.2%}" for k, v in out.items()))
    print("\nตรวจทุกรอบ (รวมทุกเกม, เฉพาะรอบจริง):")
    for name, res in out.items():
        au = sum((r["audit"] for r in res), Counter())
        crit = [c for r in res for c in r["critical"]]
        print(f"  {name:<14} " + " · ".join(f"{k} {v}" for k, v in sorted(au.items())) + f"  | critical {len(crit)}")
        for c in crit[:3]:
            print("      ", c)
    print(f"\nบอตที่ดีที่สุดในแต่ละเกม PR เฉลี่ย {statistics.mean(r['best_bot'] for r in next(iter(out.values()))):.4f}"
          f" · ชนะ A = สัดส่วนเกมที่ PR สูงกว่า starter ในโลกเดียวกัน · ±SE = ความคลาดของค่าเฉลี่ย")
    if a.json:
        Path(a.json).write_text(json.dumps({k: [{**r, "status": dict(r["status"]), "audit": dict(r["audit"])} for r in v] for k, v in out.items()},
                                           ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
