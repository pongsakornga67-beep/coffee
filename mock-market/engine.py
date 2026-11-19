"""กลไกตลาดจำลอง: ล็อต ผู้ซื้อ RFQ บอตคู่แข่ง การจับคู่ คะแนน

ทุกอย่างอยู่ใน memory · ทุกฟังก์ชันที่เปลี่ยนสถานะต้องเรียกภายใต้ Exchange.lock
"""
import copy
import json
import math
import os
import random
import threading
import time

from config import BOT_NAMES, BUYERS, DEMAND_FACTOR, ORIGINS, RULES_BASE, TYPE_SHARE

BAG = 60


class ApiError(Exception):
    def __init__(self, status, code, message, field=None, hint=None, allowed=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.field, self.hint, self.allowed = field, hint, allowed

    def body(self):
        return {"error": {"code": self.code, "message": self.message, "field": self.field,
                          "hint": self.hint, "allowed": self.allowed}}


# ---------------------------------------------------------------- สูตรมูลค่า (เหมือน appraisal.py)

def grade(rules, d):
    for step in rules["defect_steps"]:
        if d <= step["max_defect_pct"]:
            return step["grade"], step["step"]
    last = rules["defect_steps"][-1]
    return last["grade"], last["step"]


def moisture_factor(rules, m):
    mo = rules["moisture"]
    if m <= mo["ref"]:
        return 1.0
    if m <= mo["hard_limit"]:
        return (1 - m / 100) / (1 - mo["ref"] / 100) - mo["penalty_per_pp"] * (m - mo["ref"])
    return mo["hard_factor"]


def screen_factor(rules, s):
    table = {int(k): v for k, v in rules["screen_factors"].items()}
    return table[min(max(int(s), min(table)), max(table))]


def true_value(rules, q):
    d = q["defect_pct"]
    g = (1 - d / 100) * grade(rules, d)[1]
    return rules["p0"] * g * moisture_factor(rules, q["moisture_pct"]) * screen_factor(rules, q["screen"])


def spec_ok(q, spec):
    return (q["defect_pct"] <= spec["max_defect_pct"] and q["moisture_pct"] <= spec["max_moisture_pct"]
            and (spec["min_screen"] is None or q["screen"] >= spec["min_screen"]))


# ---------------------------------------------------------------- ผู้ขาย (ทีมจริง หรือ บอต)

class Seller:
    def __init__(self, sid, name, is_bot, lots, rng):
        self.id, self.name, self.is_bot = sid, name, is_bot
        self.university = "บอต" if is_bot else ""
        self.lots = lots                    # lot_id -> lot dict (มี true, sample, kg_total, kg_remaining)
        self.offers = []                    # ข้อเสนอของรอบปัจจุบัน
        self.results = {}                   # round_id -> ผลรอบ
        self.last_round_id = None
        self.revenue = 0.0
        self.kg_sold = 0
        self.appraisal = None
        self.score = None
        self.audit = []                     # ผลตรวจทุกรอบ: {round, level, code, msg}
        self.idle_rounds = {}               # lot_id -> จำนวนรอบติดกันที่ไม่ได้เสนอ
        self.mismatch_seen = set()          # (lot_id, buyer_id) ที่เคยโดน SPEC_MISMATCH
        if is_bot:
            # นิสัยบอต: steady = แบบ starter · calendar = เก็บของดีไว้ขายช่วงเทศกาล · cheap = ตัดราคาขายเร็ว
            #           greedy = ตั้งแพงขายช้า · dumper = เทขายถูกตั้งแต่ต้น
            self.policy = rng.choices(["steady", "calendar", "cheap", "greedy", "dumper"], [40, 20, 20, 15, 5])[0]
            lo_hi = {"steady": ((1.00, 1.15), (0.95, 1.05), (0.88, 0.98)), "calendar": ((1.03, 1.16), (0.96, 1.05), (0.89, 0.97)),
                     "cheap": ((0.97, 1.05), (0.91, 0.98), (0.86, 0.92)), "greedy": ((1.12, 1.25), (1.04, 1.12), (0.96, 1.03)),
                     "dumper": ((0.90, 0.95), (0.88, 0.93), (0.85, 0.89))}[self.policy]
            self.markup = {t: rng.uniform(*r) for t, r in zip(("SPECIALTY", "COMMERCIAL", "INSTANT"), lo_hi)}
            self.pace = {"steady": rng.uniform(1.1, 1.5), "calendar": 1.3, "cheap": rng.uniform(1.5, 2.0),
                         "greedy": rng.uniform(0.9, 1.2), "dumper": 3.0}[self.policy]
            self.margin = rng.choice([0.0, 0.0, 0.2, 0.4])   # บางบอตเผื่อขอบสเปก

    def kg_total(self):
        return sum(l["kg_total"] for l in self.lots.values())

    def kg_remaining(self):
        return sum(l["kg_remaining"] for l in self.lots.values())

    def open_offers(self):
        return [o for o in self.offers if o["status"] == "OPEN"]

    def kg_in_open(self, lot_id):
        return sum(o["kg"] for o in self.open_offers() if o["lot_id"] == lot_id)


# ---------------------------------------------------------------- เกมหนึ่งเกม

TEAM_META = lambda team_id: {}      # server ตั้งให้: คืน {"strategy": ...} ของทีมนั้น (ใส่ลง log.json ไว้ใช้วิเคราะห์ภายหลัง)


class Game:
    def __init__(self, cfg, seed=None, name="sandbox"):
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.name = name
        self.rules = self._rules()
        self.calendar = self._calendar()
        self.sellers = {}                   # seller id -> Seller
        self.base_lots = self._base_lots()
        self.buyers = []
        for b in BUYERS:
            if b["buyer_type"] in cfg["buyer_types"]:
                b = copy.deepcopy(b)
                b["ceiling"] = round(b["ceiling"] + self.rng.uniform(-0.03, 0.03), 3)
                self.buyers.append(b)
        self.rounds = ([{"round_id": f"P{i + 1}", "kind": "practice", "number": 0} for i in range(cfg["practice_rounds"])]
                       + [{"round_id": f"R{n}", "kind": "real", "number": n} for n in range(1, cfg["rounds"] + 1)])
        self.round_idx = -1
        self.rfqs = []
        self.offer_seq = 0
        self.pending = None                 # ผลที่จับคู่แล้วรอประกาศตอนจบ ROUND_CLEARING
        self.snapshot = None                # สต็อกก่อนรอบซ้อม (คืนหลังรอบซ้อม)
        self.paused, self.paused_at = False, None
        now = time.time()
        self.phase_started_at = now
        self.frozen_board = None
        self.rank_hist = []                 # หลังจบแต่ละรอบจริง: {round_id, ranks:{id:อันดับ}, pr:{id:PR}}
        if cfg["wait_for_start"]:
            self.phase, self.phase_ends_at = "WAITING", None
        else:
            self.phase, self.phase_ends_at = "SETUP", now + self._dur("setup_s")
        self.closed_at = None
        for i in range(cfg.get("bots", 19)):  # noqa
            k = len(BOT_NAMES)
            self._add_seller(f"BOT{i + 1:03d}", BOT_NAMES[i % k] + (f" {i // k + 1}" if i >= k else ""), True)

    # ---- สร้างเกม
    def _rules(self):
        r = copy.deepcopy(RULES_BASE)
        c = self.cfg
        r["round_capacity"] = None if c["round_capacity_factor"] is None else "ceil(factor × kg_total ÷ rounds) ต่อทีม"
        r["round_capacity_factor"] = c["round_capacity_factor"]
        r["scoring"] = {"pr_points": 80, "appraisal_points": 20, "appraisal_zero_at_mape": 0.1,
                        "leaderboard_freeze_after_round": c["leaderboard_freeze_after_round"]}
        r["timeline"] = {"setup_s": c["setup_s"], "appraisal_s": c["appraisal_s"], "open_s": c["open_s"],
                         "clearing_s": c["clearing_s"], "rounds": c["rounds"], "practice_rounds": c["practice_rounds"]}
        return r

    def _calendar(self):
        n = self.cfg["rounds"]
        if self.cfg.get("calendar"):
            cal = []
            for a, b, label, w, share in self.cfg["calendar"]:
                for r in range(a, min(b, n) + 1):
                    cal.append({"round": r, "rounds": list(range(a, b + 1)), "label": label,
                                "demand_weight": w, "specialty_share": share})
            return cal
        has_spec = "SPECIALTY" in self.cfg["buyer_types"]
        cal = []
        fest = {max(2, n // 2 - 1), max(2, n // 2)} if has_spec and n >= 6 else set()
        for r in range(1, n + 1):
            if r in fest:
                e = {"label": "เทศกาล", "demand_weight": 1.3, "specialty_share": 0.35}
            elif r == n and n >= 6:
                e = {"label": "ใกล้ปิดตลาด", "demand_weight": 0.8, "specialty_share": None}
            else:
                e = {"label": "ปกติ", "demand_weight": 1.0, "specialty_share": None}
            cal.append({"round": r, "rounds": [r], **e})
        return cal

    def _base_lots(self):
        """โจทย์ล็อตของเกม: ทุกทีมได้ล็อตแบบเดียวกัน ค่าขยับเล็กน้อยต่อทีม (เหมือนวันแข่ง)"""
        rng = self.rng
        tiers = ["premium", "mid", "mid", "low", "edge"]
        rng.shuffle(tiers)
        n_lots = self.cfg["lots_per_team"]
        bags = None
        if self.cfg.get("total_bags"):
            total, bags = self.cfg["total_bags"], [3] * n_lots      # อย่างน้อยล็อตละ 3 กระสอบ แล้วสุ่มแจกที่เหลือ
            for _ in range(total - 3 * n_lots):
                bags[rng.randrange(n_lots)] += 1
        out = []
        for i in range(self.cfg["lots_per_team"]):
            t = tiers[i % len(tiers)]
            if self.cfg.get("standard_lots"):
                # ล็อตมาตรฐาน: ค่าเสียน้อย ความชื้นต่ำ เมล็ดใหญ่ ผ่านสเปกผู้ซื้อทุกราย (ขายได้ทุกล็อต) แต่สุ่มค่าใหม่ทุกเกม
                q = {"defect_pct": rng.uniform(0.5, 2.5), "moisture_pct": rng.uniform(10.8, 12.2), "screen": rng.randint(16, 18)}
            elif t == "premium":
                q = {"defect_pct": rng.uniform(0.5, 3.2), "moisture_pct": rng.uniform(10.8, 12.6), "screen": rng.randint(15, 18)}
            elif t == "mid":
                q = {"defect_pct": rng.uniform(2.5, 6.8), "moisture_pct": rng.uniform(11.4, 13.6), "screen": rng.randint(13, 16)}
            elif t == "low":
                q = {"defect_pct": rng.uniform(6.5, 13.0), "moisture_pct": rng.uniform(12.6, 15.4), "screen": rng.randint(12, 14)}
            else:   # ชิดขอบเกรด/สเปก ให้ noise มีผล
                edge = rng.choice([3.0, 5.0, 7.0])
                q = {"defect_pct": edge + rng.uniform(-0.3, 0.3), "moisture_pct": rng.choice([12.5, 13.0, 13.5]) + rng.uniform(-0.2, 0.2),
                     "screen": rng.randint(13, 17)}
            origin, process = rng.choice(ORIGINS)
            out.append({"quality": q, "bags": bags[i] if bags else rng.randint(2, 9), "origin": origin, "process": process})
        return out

    def _make_lots(self, sid):
        std = bool(self.cfg.get("standard_lots"))
        rng, noise = self.rng, 0.0 if std else self.cfg["sample_noise"]
        order = list(range(len(self.base_lots)))
        if self.cfg["mode"] == "shared" and not std:
            rng.shuffle(order)
        lots = {}
        for k, i in enumerate(order, 1):
            b = self.base_lots[i]
            jit = 0.15 if self.cfg["mode"] == "shared" and not std else 0.0
            true = {"defect_pct": round(max(0.1, b["quality"]["defect_pct"] + rng.uniform(-jit, jit)), 2),
                    "moisture_pct": round(b["quality"]["moisture_pct"] + rng.uniform(-jit, jit), 2),
                    "screen": b["quality"]["screen"]}
            # ผลวัดตัวอย่าง 350 ก. คลาดจากของจริง (วันที่ 17 ไม่คลาด)
            sd_d = (0.25 + 0.05 * true["defect_pct"]) * noise
            sample = {"defect_pct": round(max(0.0, true["defect_pct"] + rng.gauss(0, sd_d)), 2),
                      "moisture_pct": round(true["moisture_pct"] + rng.gauss(0, 0.2 * noise), 2),
                      "screen": true["screen"] + (rng.choice([-1, 1]) if noise and rng.random() < 0.12 * noise else 0)}
            sample["screen"] = min(18, max(12, sample["screen"]))
            lot_id = f"LOT-{sid}-{k}"
            lots[lot_id] = {"lot_id": lot_id, "kg_total": b["bags"] * BAG, "kg_remaining": b["bags"] * BAG,
                            "origin": b["origin"], "process": b["process"], "sample": sample, "true": true,
                            "tv": true_value(self.rules, true)}
        return lots

    def _add_seller(self, sid, name, is_bot):
        s = Seller(sid, name, is_bot, self._make_lots(sid), self.rng)
        self.sellers[sid] = s
        return s

    def seller(self, team_id):
        s = self.sellers.get(team_id)
        if s is None:
            s = self._add_seller(team_id, team_id, False)
        return s

    # ---- เวลา
    def _dur(self, key):
        """ความยาวช่วง · หาร speed (ปุ่มเร่งเวลา) · auto_skip ทำให้ช่วงรอ (SETUP/CLEARING) สั้นลง"""
        d = self.cfg[key] / max(0.01, float(self.cfg.get("speed", 1)))
        if self.cfg.get("auto_skip") and key in ("setup_s", "clearing_s"):
            d = min(d, 3.0)
        # เร่งเวลาแค่ไหน agent ก็ต้องทันรอบ: เปิดรับอย่างน้อย 20 วิ · จับคู่อย่างน้อย 6 วิ (agent อ่านผลทันก่อนรอบถัดไป)
        floor = {"open_s": 20.0, "clearing_s": 6.0, "appraisal_s": 15.0}.get(key, 0.0)
        return max(d, min(floor, self.cfg[key]))

    def humans(self):
        return [s for s in self.sellers.values() if not s.is_bot]

    def _auto_skip(self, now):
        """ข้ามรอบเองเมื่อทุกทีม (คน/agent) ส่งครบแล้ว ไม่ต้องรอนาฬิกา"""
        if not self.cfg.get("auto_skip") or self.paused or self.phase_ends_at is None or now - self.phase_started_at < 2:
            return
        hs = self.humans()
        if not hs:
            return
        if self.phase == "APPRAISAL" and all(s.appraisal for s in hs):
            self.phase_ends_at = now
        elif self.phase == "ROUND_OPEN" and all(s.offers for s in hs) and \
                all(now - max(o["created_at"] for o in s.offers) > 1.5 for s in hs):
            self.phase_ends_at = now

    def round(self):
        return self.rounds[self.round_idx] if 0 <= self.round_idx < len(self.rounds) else None

    def seconds_left(self, now):
        if self.phase_ends_at is None:
            return None
        end = self.phase_ends_at + ((now - self.paused_at) if self.paused else 0)
        return round(max(0.0, end - now), 1)

    def set_paused(self, on, now):
        if on and not self.paused:
            self.paused, self.paused_at = True, now
        elif not on and self.paused:
            if self.phase_ends_at is not None:
                self.phase_ends_at += now - self.paused_at
            self.paused, self.paused_at = False, None

    def start(self, now):
        if self.phase == "WAITING":
            if self.cfg.get("standard_lots"):        # เปิดตลาด = สุ่มล็อตใหม่ให้ทุกทีม (ได้เหมือนกันหมด)
                self.base_lots = self._base_lots()
                for sid, s in self.sellers.items():
                    s.lots = self._make_lots(sid)
            self.phase, self.phase_ends_at, self.phase_started_at = "SETUP", now + self._dur("setup_s"), now

    def skip(self, now, force=False):
        """ปุ่มข้ามช่วง: จบช่วงนี้ทันที แต่ถ้ายังมีทีมที่ยังไม่ได้ส่ง/ยังไม่ได้อ่านผล ให้เวลาอีกนิด (agent จะได้ไม่พลาดรอบ)"""
        if self.phase == "WAITING":
            self.start(now)
        elif self.phase_ends_at is not None:
            end = now
            hs = self.humans()
            if not force and hs:
                since = now - self.phase_started_at
                if self.phase == "ROUND_OPEN" and not all(s.offers for s in hs):
                    end = max(now, self.phase_started_at + 15)          # รอ agent ส่งได้สูงสุด 15 วิหลังรอบเปิด
                elif self.phase == "APPRAISAL" and not all(s.appraisal for s in hs):
                    end = max(now, self.phase_started_at + 15)
                elif self.phase == "ROUND_CLEARING" and since < 6:
                    end = self.phase_started_at + 6                     # ให้ agent อ่านผลรอบก่อน
            self.phase_ends_at = min(self.phase_ends_at, end)
            if self.paused:
                self.paused_at = now
        self.tick(now)

    def tick(self, now):
        self._auto_skip(now)
        guard = 0
        while not self.paused and self.phase_ends_at is not None and now >= self.phase_ends_at and guard < 50:
            guard += 1
            self._advance(self.phase_ends_at)
            self.phase_started_at = now

    def _advance(self, at):
        c = self.cfg
        if self.phase == "SETUP":
            self.phase, self.phase_ends_at = "APPRAISAL", at + self._dur("appraisal_s")
            for s in self.sellers.values():          # บอตส่งผลประเมินจากผลวัดตัวอย่าง (คะแนนประเมินเทียบกับทีมได้)
                if s.is_bot and s.appraisal is None:
                    by = {lid: round(true_value(self.rules, l["sample"]) * (1 + self.rng.gauss(0, 0.01)), 2) for lid, l in s.lots.items()}
                    s.appraisal = {"submitted_at": at, "by_lot": by,
                                   "items": [{"lot_id": k, "grade": None, "est_value_per_kg": v} for k, v in by.items()]}
        elif self.phase == "APPRAISAL" or (self.phase == "ROUND_CLEARING"):
            if self.phase == "ROUND_CLEARING":
                self._publish()
            if self.round_idx + 1 >= len(self.rounds):
                self._close(at)
                return
            self.round_idx += 1
            self._open_round()
            self.phase, self.phase_ends_at = "ROUND_OPEN", at + self._dur("open_s")
        elif self.phase == "ROUND_OPEN":
            self._clear()
            self.phase, self.phase_ends_at = "ROUND_CLEARING", at + self._dur("clearing_s")
        elif self.phase == "CLOSED":
            self.phase_ends_at = None       # Exchange สร้างเกมใหม่เอง (sandbox)

    # ---- รอบ
    def cal_entry(self, rnd):
        if rnd["kind"] != "real":
            return {"label": "ซ้อม", "demand_weight": 1.0, "specialty_share": None}
        return self.calendar[rnd["number"] - 1]

    def capacity_kg(self, s):
        f = self.cfg["round_capacity_factor"]
        if f is None:
            return None
        return max(BAG, math.ceil(f * s.kg_total() / self.cfg["rounds"] / BAG) * BAG)

    def capacity_left(self, s):
        cap = self.capacity_kg(s)
        if cap is None:
            return None
        return max(0, cap - sum(o["kg"] for o in s.open_offers()))

    def _open_round(self):
        rnd, rng = self.round(), self.rng
        if rnd["kind"] == "practice" and self.snapshot is None:
            self.snapshot = {sid: ({lid: l["kg_remaining"] for lid, l in s.lots.items()}, s.revenue, s.kg_sold)
                             for sid, s in self.sellers.items()}
        cal = self.cal_entry(rnd)
        supply = sum(s.kg_total() for s in self.sellers.values()) / BAG / self.cfg["rounds"]
        total = max(4, round(supply * cal["demand_weight"] * rng.uniform(*DEMAND_FACTOR)))
        types = self.cfg["buyer_types"]
        share = {t: TYPE_SHARE[t] for t in types}
        if cal.get("specialty_share") and "SPECIALTY" in share:
            share["SPECIALTY"] = cal["specialty_share"]
        tot = sum(share.values())
        rfqs = []
        for t in types:
            bags = max(1, round(total * share[t] / tot))
            buyers = [b for b in self.buyers if b["buyer_type"] == t]
            n = min(8, max(1, math.ceil(bags / 25)))
            cuts = sorted(rng.sample(range(1, bags), min(n - 1, bags - 1))) if bags > 1 else []
            parts = [b - a for a, b in zip([0] + cuts, cuts + [bags])]
            for p in parts:
                b = rng.choice(buyers)
                rfqs.append({"buyer": b, "bags": p})
        rng.shuffle(rfqs)
        self.rfqs = []
        for i, r in enumerate(rfqs, 1):
            b = r["buyer"]
            self.rfqs.append({"rfq_id": f"{rnd['round_id']}-{i:02d}", "buyer": b["buyer"], "buyer_id": b["buyer_id"],
                              "buyer_type": b["buyer_type"], "kg": r["bags"] * BAG, "bags": r["bags"],
                              "spec": dict(b["spec"]),
                              "_ceiling": b["ceiling"] + rng.uniform(-0.01, 0.01)
                              + (0.03 if cal["label"] == "เทศกาล" and b["buyer_type"] == "SPECIALTY" else 0)})
        for s in self.sellers.values():
            s.offers = []
        for s in self.sellers.values():
            if s.is_bot:
                self._bot_offers(s)

    def _bot_offers(self, s):
        rng, rnd = self.rng, self.round()
        left_rounds = self.cfg["rounds"] if rnd["kind"] == "practice" else self.cfg["rounds"] - rnd["number"] + 1
        room = self.capacity_left(s)
        room = 10 ** 9 if room is None else room
        rfq_left = {r["rfq_id"]: r["kg"] for r in self.rfqs}
        rfqs = sorted(self.rfqs, key=lambda r: -(s.markup[r["buyer_type"]] + rng.uniform(0, 0.08)))
        weight = 1.0
        if s.policy == "calendar" and rnd["kind"] == "real":
            ahead = [c["demand_weight"] for c in self.calendar[rnd["number"] - 1:]]
            weight = ahead[0] / (sum(ahead) / len(ahead))
        spec_buyers = [b for b in self.buyers if b["buyer_type"] == "SPECIALTY"]
        fest_ahead = rnd["kind"] == "real" and any(c["label"].startswith("เทศกาล") for c in self.calendar[rnd["number"]:])
        for lot in sorted(s.lots.values(), key=lambda l: -l["tv"]):
            avail = lot["kg_remaining"] // BAG * BAG
            w = weight
            if s.policy == "calendar" and fest_ahead and self.cal_entry(rnd)["label"] == "ปกติ" \
                    and any(spec_ok(lot["sample"], b["spec"]) for b in spec_buyers):
                w = 0.3                                     # เก็บล็อตเกรด Specialty ไว้รอเทศกาล
            budget = avail if left_rounds <= 1 else min(avail, math.ceil(avail / left_rounds * s.pace * w / BAG) * BAG)
            m = s.margin
            for r in rfqs:
                if budget < BAG or room < BAG:
                    break
                sp = r["spec"]
                ok = (lot["sample"]["defect_pct"] <= sp["max_defect_pct"] - m and lot["sample"]["moisture_pct"] <= sp["max_moisture_pct"] - m / 2
                      and (sp["min_screen"] is None or lot["sample"]["screen"] >= sp["min_screen"]))
                kg = min(budget, rfq_left[r["rfq_id"]], room) // BAG * BAG
                if not ok or kg < BAG:
                    continue
                est = true_value(self.rules, lot["sample"])
                price = round(est * min(1.3, max(0.85, s.markup[r["buyer_type"]] + rng.uniform(-0.02, 0.02))), 2)
                self._new_offer(s, {"rfq_id": r["rfq_id"], "lot_id": lot["lot_id"], "kg": kg, "price_per_kg": price})
                budget -= kg
                room -= kg
                rfq_left[r["rfq_id"]] -= kg

    def _bot_learn(self, s, result):
        types = {r["rfq_id"]: r["buyer_type"] for r in result["market_summary"]}
        for o in result["my_offers"]:
            t = types.get(o["rfq_id"])
            step = {"ACCEPTED": 0.015, "PARTIAL": 0.0, "OUTBID": -0.015, "ABOVE_LIMIT": -0.04}.get(o["status"], 0)
            if t:
                s.markup[t] = min(1.3, max(0.85, s.markup[t] + step))

    def _new_offer(self, s, o):
        self.offer_seq += 1
        offer = {"offer_id": f"O{self.offer_seq:05d}", "round_id": self.round()["round_id"], "rfq_id": o["rfq_id"],
                 "lot_id": o["lot_id"], "kg": int(o["kg"]), "price_per_kg": round(float(o["price_per_kg"]), 2),
                 "status": "OPEN", "filled_kg": 0, "created_at": round(time.time(), 3)}
        s.offers.append(offer)
        return offer

    # ---- จับคู่
    def _clear(self):
        rnd = self.round()
        by_rfq = {r["rfq_id"]: [] for r in self.rfqs}
        for s in self.sellers.values():
            for o in s.open_offers():
                by_rfq.setdefault(o["rfq_id"], []).append((s, o))
        summary = []
        for r in self.rfqs:
            live = []
            for s, o in by_rfq[r["rfq_id"]]:
                lot = s.lots[o["lot_id"]]
                if not spec_ok(lot["true"], r["spec"]):
                    o["status"] = "SPEC_MISMATCH"
                elif o["price_per_kg"] / lot["tv"] > r["_ceiling"]:
                    o["status"] = "ABOVE_LIMIT"
                else:
                    live.append((round(o["price_per_kg"] / lot["tv"], 6), s, o))
            live.sort(key=lambda x: x[0])
            want = r["kg"] // BAG
            i = 0
            while i < len(live):
                j = i
                while j < len(live) and live[j][0] == live[i][0]:
                    j += 1
                group = [x[2] for x in live[i:j]]
                need = {id(o): o["kg"] // BAG for o in group}
                got = {id(o): 0 for o in group}
                while want > 0 and any(need[k] > got[k] for k in need):
                    for o in group:                     # คุ้มเท่ากัน แบ่งกันทีละกระสอบ
                        if want > 0 and got[id(o)] < need[id(o)]:
                            got[id(o)] += 1
                            want -= 1
                for o in group:
                    o["filled_kg"] = got[id(o)] * BAG
                i = j
            bought, revenue, prices = 0, 0.0, []
            for _, s, o in live:
                o["status"] = "ACCEPTED" if o["filled_kg"] == o["kg"] else ("PARTIAL" if o["filled_kg"] else "OUTBID")
                if o["filled_kg"]:
                    bought += o["filled_kg"]
                    revenue += o["filled_kg"] * o["price_per_kg"]
                    prices.append(o["price_per_kg"])
                    lot = s.lots[o["lot_id"]]
                    lot["kg_remaining"] -= o["filled_kg"]
                    if rnd["kind"] == "real":
                        s.revenue += o["filled_kg"] * o["price_per_kg"]
                        s.kg_sold += o["filled_kg"]
            summary.append({"rfq_id": r["rfq_id"], "buyer": r["buyer"], "buyer_id": r["buyer_id"], "buyer_type": r["buyer_type"],
                            "wanted_kg": r["kg"], "bought_kg": bought, "offers": len(by_rfq[r["rfq_id"]]),
                            "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
                            "price_avg": round(revenue / bought, 2) if bought else None})
        self.pending = {"round_id": rnd["round_id"], "kind": rnd["kind"], "summary": summary,
                        "mine": {sid: [dict(o) for o in s.offers if o["status"] != "CANCELLED"] for sid, s in self.sellers.items()}}
        for s in self.sellers.values():
            for o in s.offers:
                if o["status"] == "OPEN":
                    o["status"] = "OUTBID"

    def _publish(self):
        p = self.pending
        if not p:
            return
        for s in self.humans():
            try:
                self._audit_round(s, p)
            except Exception as e:          # noqa: BLE001 — ตัวตรวจห้ามทำให้ตลาดล่ม
                s.audit.append({"round": p["round_id"], "level": "info", "code": "AUDIT_ERROR", "msg": repr(e)})
        for sid, s in self.sellers.items():
            res = {"round_id": p["round_id"], "kind": p["kind"],
                   "my_offers": [{k: o[k] for k in ("offer_id", "rfq_id", "lot_id", "kg", "price_per_kg", "status", "filled_kg")}
                                 for o in p["mine"].get(sid, [])],
                   "market_summary": [dict(x) for x in p["summary"]]}
            s.results[p["round_id"]] = res
            s.last_round_id = p["round_id"]
            if s.is_bot:
                self._bot_learn(s, res)
        if p["kind"] == "practice" and self.snapshot and not (self.round_idx + 1 < len(self.rounds)
                                                              and self.rounds[self.round_idx + 1]["kind"] == "practice"):
            for sid, (kgs, rev, sold) in self.snapshot.items():   # รอบซ้อมไม่นับ: คืนสต็อก
                s = self.sellers[sid]
                for lid, kg in kgs.items():
                    s.lots[lid]["kg_remaining"] = kg
                s.revenue, s.kg_sold = rev, sold
            self.snapshot = None
        for s in self.sellers.values():
            s.offers = []
        if p["kind"] == "real":
            rk = self.ranking()
            self.rank_hist.append({"round_id": p["round_id"], "number": self.round()["number"],
                                   "ranks": {x.id: i for i, x in enumerate(rk, 1)},
                                   "pr": {x.id: round(self.live_pr(x), 4) for x in rk}})
        if p["kind"] == "real" and self.round()["number"] == self.cfg["leaderboard_freeze_after_round"]:
            self.frozen_board = [s.id for s in self.ranking()]     # จออันดับหยุดอัปเดตหลังรอบนี้
        self.pending = None

    def live_pr(self, s):
        """PR ถ้าตลาดปิดตอนนี้ = (รายได้ + 0.8 × TV จริงที่เหลือ) ÷ TV ทั้งหมด"""
        tv = sum(l["kg_total"] * l["tv"] for l in s.lots.values())
        left = sum(l["kg_remaining"] * l["tv"] for l in s.lots.values()) * self.rules["salvage_ratio"]
        return (s.revenue + left) / tv if tv else 0.0

    def rank_change(self, sid):
        """อันดับขึ้น(+)/ลง(−) เทียบรอบก่อนหน้า · รอบแรกยังไม่มีให้เทียบ → None"""
        h = self.rank_hist
        if not h or sid not in h[-1]["ranks"]:
            return None
        cur = h[-1]
        if len(h) < 2 or sid not in h[-2]["ranks"]:
            return {"round_id": cur["round_id"], "rank": cur["ranks"][sid], "rank_delta": None, "pr_delta": None}
        prev = h[-2]
        return {"round_id": cur["round_id"], "rank": cur["ranks"][sid], "rank_delta": prev["ranks"][sid] - cur["ranks"][sid],
                "pr_delta": round(cur["pr"][sid] - prev["pr"][sid], 4)}

    def ranking(self):
        """อันดับ: ระหว่างแข่งใช้ PR ถ้าปิดตอนนี้ · ปิดแล้วใช้คะแนนรวม"""
        if self.phase == "CLOSED" and all(s.score for s in self.sellers.values()):
            rows = sorted(self.sellers.values(), key=lambda s: -s.score["total"])
        else:
            rows = sorted(self.sellers.values(), key=lambda s: -self.live_pr(s))
        return rows

    def _audit_round(self, s, p):
        """ตรวจข้อเสนอของทีม (คน/agent) หลังจับคู่ทุกรอบ · ใช้ทั้งหน้าเว็บและ benchmark/audit"""
        rid, mine = p["round_id"], p["mine"].get(s.id, [])
        rfq = {r["rfq_id"]: r for r in self.rfqs}
        cap = self.capacity_kg(s)
        add = lambda lvl, code, msg: s.audit.append({"round": rid, "level": lvl, "code": code, "msg": msg})
        # สต็อกตอนเปิดรอบ = ที่เหลือตอนนี้ + ที่เพิ่งขายได้ในรอบนี้
        sold = {}
        for o in mine:
            sold[o["lot_id"]] = sold.get(o["lot_id"], 0) + o["filled_kg"]
        stock = {lid: l["kg_remaining"] + sold.get(lid, 0) for lid, l in s.lots.items()}
        # ขายได้ = ตัวอย่างผ่านสเปกของ RFQ ที่ล็อตนั้นยังไม่เคยโดน SPEC_MISMATCH กับผู้ซื้อรายนั้น (โดนแล้ว = รู้ว่าของจริงไม่ผ่าน)
        sellable = {lid for lid, kg in stock.items() if kg >= BAG and any(
            spec_ok(s.lots[lid]["sample"], r["spec"]) and (lid, r["buyer_id"]) not in s.mismatch_seen for r in self.rfqs)}
        offered_kg = sum(o["kg"] for o in mine)
        if not mine:
            if sellable:
                add("critical", "MISSED_ROUND", f"ไม่ได้ส่งข้อเสนอเลย ทั้งที่มีของขายได้ {len(sellable)} ล็อต")
        else:
            room = cap if cap is not None else 10 ** 9
            can = min(room, sum(stock[l] for l in sellable) // BAG * BAG)
            if can - offered_kg >= BAG:
                add("warn", "UNUSED_CAPACITY", f"เสนอ {offered_kg // BAG} จากที่เสนอได้ {can // BAG} กระสอบ")
        offered_lots = {o["lot_id"] for o in mine}
        for lid in sellable:
            s.idle_rounds[lid] = 0 if lid in offered_lots else s.idle_rounds.get(lid, 0) + 1
            if s.idle_rounds[lid] >= 3 and p["kind"] == "real":
                add("warn", "STARVED_LOT", f"{lid} เหลือ {stock[lid] // BAG} กระสอบ ไม่ได้เสนอมา {s.idle_rounds[lid]} รอบติด")
        for o in mine:
            r = rfq.get(o["rfq_id"])
            bid = r["buyer_id"] if r else "?"
            lot = s.lots[o["lot_id"]]
            if o["status"] == "SPEC_MISMATCH":
                key = (o["lot_id"], bid)
                if key in s.mismatch_seen:
                    add("critical", "REPEAT_MISMATCH", f"{o['lot_id']} โดน SPEC_MISMATCH กับ {r and r['buyer']} ซ้ำ (เคยโดนแล้ว)")
                else:
                    add("warn", "SPEC_MISMATCH", f"{o['lot_id']} ของจริงไม่ผ่านสเปก {r and r['buyer']}")
                s.mismatch_seen.add(key)
            elif o["status"] == "ABOVE_LIMIT":
                add("info", "ABOVE_LIMIT", f"{o['lot_id']} ×{o['price_per_kg'] / lot['tv']:.3f} TV จริง แพงเกินเพดาน {r and r['buyer']}")
            if o["filled_kg"] and o["price_per_kg"] < 0.80 * lot["tv"]:
                est = true_value(self.rules, lot["sample"])
                if o["price_per_kg"] >= 0.85 * est:     # ตั้งราคาสมเหตุสมผลแล้ว แต่ตัวอย่างประเมินต่ำกว่าของจริง (รู้ไม่ได้ล่วงหน้า)
                    add("warn", "SAMPLE_UNDERVALUED", f"{o['lot_id']} ขาย ×{o['price_per_kg'] / lot['tv']:.3f} TV จริง (ตัวอย่างประเมินต่ำกว่าของจริง)")
                else:
                    add("critical", "BELOW_SALVAGE", f"{o['lot_id']} ขาย ×{o['price_per_kg'] / lot['tv']:.3f} TV จริง ต่ำกว่าเก็บไว้ (0.80)")
        if p["kind"] == "real" and self.round()["number"] == self.cfg["rounds"]:
            left = sum(l["kg_remaining"] for l in s.lots.values())
            if left:
                lost = sum(l["kg_remaining"] * l["tv"] * 0.2 for l in s.lots.values())
                add("info", "END_UNSOLD", f"จบเกมเหลือ {left // BAG} กระสอบ (เสียโอกาสราว {lost:,.0f} บาท เทียบขายที่ TV)")

    def _close(self, at):
        self.phase, self.round_idx = "CLOSED", len(self.rounds) - 1
        self.rfqs = []
        ra = self.cfg.get("restart_after_s")
        self.phase_ends_at = at + ra if ra else None
        self.closed_at = at
        self._score()
        try:
            self.write_log()
        except Exception as e:              # noqa: BLE001 — เขียนล็อกพลาดห้ามทำให้ตลาดล่ม
            print(f"[log.json] เขียนไม่สำเร็จ: {e!r}", flush=True)

    def export_log(self):
        """สรุปทั้งเกมเป็น dict: ทุกรอบ · ข้อเสนอ/ผลของทีมคน · ตลาดรวม · คะแนน · ผลตรวจ"""
        rows = sorted(self.sellers.values(), key=lambda s: -(s.score or {}).get("total", 0))
        rank = {s.id: i + 1 for i, s in enumerate(rows)}
        teams = {}
        for s in self.humans():
            rounds = []
            for rd in self.rounds:
                res = s.results.get(rd["round_id"])
                if res:
                    rounds.append({"round_id": rd["round_id"], "kind": rd["kind"], "my_offers": res["my_offers"],
                                   "kg_filled": sum(o["filled_kg"] for o in res["my_offers"]),
                                   "revenue_thb": round(sum(o["filled_kg"] * o["price_per_kg"] for o in res["my_offers"]), 2)})
            teams[s.id] = {"team_name": s.name, "rank": rank[s.id], "score": s.score, "revenue_thb": round(s.revenue, 2),
                           "kg_sold": s.kg_sold, "kg_remaining": s.kg_remaining(), "kg_total": s.kg_total(),
                           "appraisal": s.appraisal,
                           "lots": {lid: {"kg_total": l["kg_total"], "kg_remaining": l["kg_remaining"],
                                          "tv_true": round(l["tv"], 2), "sample": l["sample"]} for lid, l in s.lots.items()},
                           "rounds": rounds, "audit": s.audit,
                           "rank_by_round": self._rank_rows(s.id)}
        any_s = next(iter(self.sellers.values()))
        market = [{"round_id": rid, "kind": r["kind"], "rfqs": r["market_summary"]} for rid, r in any_s.results.items()]
        summary, offers = self._analysis_rows(rank, any_s)
        return {"summary": summary, "offers": offers, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "market": self.name, "n_teams": len(self.sellers),
                "leaderboard": [{"rank": i + 1, "team_id": s.id, "team_name": s.name, "bot": s.is_bot, **(s.score or {})} for i, s in enumerate(rows)],
                "rank_history": [{"round_id": h["round_id"], "ranks": h["ranks"], "pr": h["pr"]} for h in self.rank_hist],
                "teams": teams, "market_rounds": market}

    def _analysis_rows(self, rank, any_s):
        """ตารางแบนไว้วิเคราะห์ข้ามเกม: summary = 1 แถวต่อทีมคน · offers = 1 แถวต่อข้อเสนอ (มีผู้ซื้อรายคน ×TV จริง ราคาตลาด)"""
        summary, offers = [], []
        for s in self.humans():
            try:
                meta = TEAM_META(s.id) or {}
            except Exception:  # noqa: BLE001
                meta = {}
            strat = meta.get("strategy")
            stat, mk = {}, []
            for rd in self.rounds:
                res = s.results.get(rd["round_id"])
                if not res or res["kind"] != "real":
                    continue
                mkt = {r["rfq_id"]: r for r in any_s.results.get(rd["round_id"], {}).get("market_summary", [])}
                for o in res["my_offers"]:
                    lot = s.lots.get(o["lot_id"], {})
                    tv = lot.get("tv") or 0
                    q = mkt.get(o["rfq_id"], {})
                    ratio = round(o["price_per_kg"] / tv, 4) if tv else None
                    stat[o["status"]] = stat.get(o["status"], 0) + 1
                    if o["filled_kg"] and ratio:
                        mk.append(ratio)
                    offers.append({"team_id": s.id, "strategy": strat, "round_id": rd["round_id"], "lot_id": o["lot_id"],
                                   "rfq_id": o["rfq_id"], "buyer_id": q.get("buyer_id"), "buyer_type": q.get("buyer_type"),
                                   "kg": o["kg"], "price_per_kg": o["price_per_kg"], "tv_true": round(tv, 2), "x_tv": ratio,
                                   "status": o["status"], "filled_kg": o["filled_kg"],
                                   "mkt_price_avg": q.get("price_avg"), "mkt_price_min": q.get("price_min"), "mkt_price_max": q.get("price_max")})
            audit = {}
            for a in s.audit:
                if str(a["round"]).startswith("R"):
                    audit[a["code"]] = audit.get(a["code"], 0) + 1
            summary.append({"team_id": s.id, "team_name": s.name, "strategy": strat, "rank": rank[s.id],
                            "pr": (s.score or {}).get("pr"), "total": (s.score or {}).get("total"), "mape": (s.score or {}).get("mape"),
                            "kg_sold": s.kg_sold, "kg_remaining": s.kg_remaining(), "kg_total": s.kg_total(),
                            "x_tv_sold_avg": round(sum(mk) / len(mk), 4) if mk else None, "offer_status": stat, "audit": audit,
                            "seed": getattr(self, "seed", None), "rounds": len([r for r in self.rounds if r["kind"] == "real"])})
        return summary, offers

    def _rank_rows(self, sid):
        """อันดับ/PR ของทีมหลังจบแต่ละรอบ + ที่เปลี่ยนจากรอบก่อน (ทีมที่เข้ากลางเกมไม่มีรอบก่อน → None)"""
        rows, prev = [], None
        for h in self.rank_hist:
            if sid not in h["ranks"]:
                continue
            r, pr = h["ranks"][sid], h["pr"][sid]
            rows.append({"round_id": h["round_id"], "rank": r, "pr": pr,
                         "rank_delta": None if prev is None else prev[0] - r,
                         "pr_delta": None if prev is None else round(pr - prev[1], 4)})
            prev = (r, pr)
        return rows

    def write_log(self, partial=False):
        """เขียน log.json (ทับของเดิม) + สำเนาเก็บตามเวลาใน logs/ เมื่อตลาดปิด"""
        if os.environ.get("MOCK_NO_LOG"):
            return
        here = os.path.dirname(os.path.abspath(__file__))
        data = json.dumps(self.export_log(), ensure_ascii=False, indent=2, default=str)
        paths = [os.path.join(here, "logs", time.strftime("log-%Y%m%d-%H%M%S.json"))]
        if partial:                                   # เกมที่ถูกรีสตาร์ตกลางทาง: เก็บเฉพาะสำเนาใน logs/ ไม่ทับ log.json ของเกมที่จบสมบูรณ์
            paths = [paths[0].replace("log-", "log-ค้าง-")]
        else:
            cur = os.path.join(here, "log.json")
            if os.path.exists(cur):                   # เกมก่อนหน้า → เก็บเป็น log-เกมที่แล้ว.json (เหมือนที่เคยคัดลอกเอง)
                try:
                    import shutil
                    shutil.copyfile(cur, os.path.join(here, "log-เกมที่แล้ว.json"))
                except OSError:
                    pass
            ldir = os.path.join(here, "logs")
            n = len([f for f in os.listdir(ldir) if f.startswith("log-เกม")]) + 1 if os.path.isdir(ldir) else 1
            paths.insert(0, cur)
            paths.append(os.path.join(ldir, time.strftime(f"log-เกม{n}-%H%M.json")))   # ตัวอย่างชื่อ: log-เกม2-1615.json
        for path in paths:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
        print(f"[log] บันทึกผล{'ที่เล่นค้าง' if partial else 'ทั้ง 15 รอบ'}แล้ว → {paths[0]}", flush=True)

    def _score(self):
        sal = self.rules["salvage_ratio"]
        for s in self.sellers.values():
            tv_total = sum(l["kg_total"] * l["tv"] for l in s.lots.values())
            salvage = sum(l["kg_remaining"] * l["tv"] * sal for l in s.lots.values())
            pr = (s.revenue + salvage) / tv_total if tv_total else 0
            mape = None
            if s.appraisal:
                errs = []
                for lid, l in s.lots.items():
                    est = s.appraisal["by_lot"].get(lid)
                    errs.append(1.0 if est is None else abs(est - l["tv"]) / l["tv"])
                mape = sum(errs) / len(errs)
            s.score = {"pr": round(pr, 4), "mape": None if mape is None else round(mape, 4),
                       "revenue_thb": round(s.revenue, 2), "salvage_thb": round(salvage, 2),
                       "total_true_value_thb": round(tv_total, 2),
                       "appraisal_score": 0.0 if mape is None else round(20 * max(0.0, 1 - mape / 0.10), 2)}
        best = max(s.score["pr"] for s in self.sellers.values())
        for s in self.sellers.values():
            # ตลาดจริงคิดคะแนนขายเทียบคู่แข่ง สูตรไม่เปิดเผย: ที่นี่ให้ PR สูงสุดในเกม = 80, PR 0.80 = 0
            prs = 80 * max(0.0, min(1.0, (s.score["pr"] - 0.8) / (best - 0.8))) if best > 0.8 else 0.0
            s.score["pr_score"] = round(prs, 2)
            s.score["total"] = round(prs + s.score["appraisal_score"], 2)

    # ---- คำสั่งจากทีม
    def offer_errors(self, s, o, open_kg_by_lot, room):
        """ตรวจข้อเสนอ 1 รายการ · คืน ApiError หรือ None"""
        if not isinstance(o, dict):
            return ApiError(400, "INVALID_FIELD", "Each offer must be an object.")
        for f in ("rfq_id", "lot_id", "kg", "price_per_kg"):
            if f not in o or o[f] is None:
                return ApiError(400, "INVALID_FIELD", f"Missing field '{f}'.", field=f,
                                hint="An offer needs rfq_id, lot_id, kg and price_per_kg.")
        rfq = next((r for r in self.rfqs if r["rfq_id"] == o["rfq_id"]), None)
        if rfq is None:
            return ApiError(404, "UNKNOWN_RFQ", f"RFQ {o['rfq_id']} is not open in this round.", field="rfq_id",
                            hint="Use an rfq_id from state.rfqs of the current round.",
                            allowed={"rfq_ids": [r["rfq_id"] for r in self.rfqs]})
        lot = s.lots.get(o["lot_id"])
        if lot is None:
            other = any(o["lot_id"] in x.lots for x in self.sellers.values())
            return ApiError(404, "NOT_YOUR_LOT" if other else "UNKNOWN_LOT",
                            f"Lot {o['lot_id']} " + ("belongs to another team." if other else "does not exist."),
                            field="lot_id", hint="Use a lot_id from state.lots.", allowed={"lot_ids": list(s.lots)})
        kg, price = o["kg"], o["price_per_kg"]
        if isinstance(kg, bool) or not isinstance(kg, (int, float)) or kg != int(kg) or kg <= 0:
            return ApiError(400, "INVALID_FIELD", "kg must be a positive whole number of kilograms.", field="kg")
        kg = int(kg)
        if kg % BAG:
            lo = kg // BAG * BAG
            return ApiError(400, "NOT_MULTIPLE_OF_BAG", f"kg must be a multiple of {BAG} (one bag).", field="kg",
                            hint=f"Use {lo or BAG} or {lo + BAG}.", allowed={"nearest": [x for x in (lo, lo + BAG) if x > 0]})
        if kg > rfq["kg"]:
            return ApiError(400, "INVALID_FIELD", f"{rfq['rfq_id']} wants only {rfq['kg']} kg.", field="kg",
                            hint=f"Reduce kg to {rfq['kg']} or less.", allowed={"max_kg": rfq["kg"]})
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not (0 < price <= 1000):
            return ApiError(400, "INVALID_FIELD", "price_per_kg must be more than 0 and at most 1000.", field="price_per_kg",
                            allowed={"min_exclusive": 0, "max": 1000})
        used = open_kg_by_lot.get(lot["lot_id"], 0)
        free = lot["kg_remaining"] - used
        if kg > free:
            return ApiError(400, "EXCEEDS_REMAINING",
                            f"{lot['lot_id']} has {lot['kg_remaining']} kg left; open offers already use {used} kg. "
                            f"You can offer at most {max(0, free)} kg more.", field="kg",
                            hint=f"Reduce kg to {max(0, free)} or cancel another offer on {lot['lot_id']}.",
                            allowed={"max_kg": max(0, free)})
        if room is not None and kg > room:
            return ApiError(400, "OVER_ROUND_CAPACITY", f"This round your team can offer at most {room} kg more.",
                            field="kg", hint="Check capacity_left_kg in GET /v1/state before sending.", allowed={"max_kg": room})
        return None

    def check_phase(self, what="offers"):
        if self.paused:
            raise ApiError(409, "WRONG_PHASE", "The market is paused.", hint="Wait until paused is false.",
                           allowed={"phase": self.phase, "paused": True})
        if self.phase == "ROUND_CLEARING":
            raise ApiError(409, "ROUND_CLOSED", "This round no longer accepts offers.", hint="Wait for the next ROUND_OPEN.")
        if self.phase != "ROUND_OPEN":
            raise ApiError(409, "WRONG_PHASE", f"You can send {what} only in ROUND_OPEN; the market is in {self.phase}.",
                           hint="Read phase from GET /v1/state.",
                           allowed={"phase": self.phase, "phase_ends_at": self.phase_ends_at})

    def place(self, s, offers, replace=False):
        self.check_phase()
        keep = [] if replace else s.open_offers()
        used = {}
        for o in keep:
            used[o["lot_id"]] = used.get(o["lot_id"], 0) + o["kg"]
        cap = self.capacity_kg(s)
        room = None if cap is None else cap - sum(o["kg"] for o in keep)
        errors, ok = [], []
        for i, o in enumerate(offers):
            e = self.offer_errors(s, o, used, room)
            if e:
                errors.append({"index": i, "code": e.code, "message": e.message, "field": e.field, "allowed": e.allowed})
                continue
            used[o["lot_id"]] = used.get(o["lot_id"], 0) + int(o["kg"])
            if room is not None:
                room -= int(o["kg"])
            ok.append(o)
        return errors, ok

    def commit(self, s, offers, replace=False):
        cancelled = 0
        if replace:
            for o in s.open_offers():
                o["status"] = "CANCELLED"
                cancelled += 1
        return [self._new_offer(s, o) for o in offers], cancelled


# ---------------------------------------------------------------- ตลาด: จัดการหลายเกม/หลายทีม

class Exchange:
    def __init__(self, cfg, seed=None):
        self.cfg, self.seed = cfg, seed
        self.lock = threading.RLock()
        self.games = {}                     # key: "shared" หรือ team_id
        self.profiles = {}
        self.game_no = 0

    def _new_game(self, key):
        self.game_no += 1
        seed = None if self.seed is None else self.seed + self.game_no
        return Game(self.cfg, seed, name=self.cfg.get("name") or ("sandbox" if self.cfg["reveal"] else "contest"))

    def game_for(self, team_id, now=None):
        now = now or time.time()
        key = "shared" if self.cfg["mode"] == "shared" else team_id
        g = self.games.get(key)
        if g is None:
            g = self.games[key] = self._new_game(key)
        g.tick(now)
        if g.phase == "CLOSED" and g.phase_ends_at is None and g.closed_at and self.cfg.get("restart_after_s") \
                and now >= g.closed_at + self.cfg["restart_after_s"]:
            g = self.games[key] = self._new_game(key)
        g.seller(team_id)
        return g

    def reset(self, team_id):
        key = "shared" if self.cfg["mode"] == "shared" else team_id
        old = self.games.get(key)
        g = self.games[key] = self._new_game(key)
        if old:
            for sid, s in old.sellers.items():
                if not s.is_bot:
                    g.seller(sid)
        if team_id:
            g.seller(team_id)
        return g

    def tick_all(self):
        now = time.time()
        with self.lock:
            for key in list(self.games):
                g = self.games[key]
                g.tick(now)
                if g.phase == "CLOSED" and g.closed_at and self.cfg.get("restart_after_s") \
                        and now >= g.closed_at + self.cfg["restart_after_s"]:
                    teams = [sid for sid, s in g.sellers.items() if not s.is_bot]
                    g = self.games[key] = self._new_game(key)
                    for t in teams:
                        g.seller(t)
