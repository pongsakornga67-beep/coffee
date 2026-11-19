"""รัน starter agent (main.py ของทีม) กับตลาดจำลอง โดยไม่แตะ .env และไม่ปน log จริง

    python mock-market/run_agent.py                 # เล่นจนตลาดปิด (ใช้ LLM ปลอมของตลาดจำลอง)
    python mock-market/run_agent.py --forever       # เล่นต่อเกมใหม่ไปเรื่อยๆ
    python mock-market/run_agent.py --rules         # USE_LLM=0 กฎล้วน
    python mock-market/run_agent.py --key sk-T02-b  # อีกทีม (เปิดหลายหน้าต่างแข่งกันเองได้ใน --mode shared)
    python mock-market/run_agent.py --ollama        # ใช้ Ollama บนเครื่อง (ฝึกนอกเวลาแข่งเท่านั้น)

log ไปที่ mock-market/agent-logs/ แทน logs/ — logs/ จริงของทีมจะได้มีแต่ของตลาดจริงตอนส่งกรรมการ
"""
import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STARTER = HERE.parent

p = argparse.ArgumentParser()
p.add_argument("--port", type=int, default=9000)
p.add_argument("--url", help="ตลาดจำลองบนเครื่องเพื่อน เช่น http://192.168.1.20:9000 (แทน --port)")
p.add_argument("--strategy", help="ใช้กลยุทธ์จาก mock-market/strategies/<ชื่อ>.py แทน strategy.py ของทีม (ไว้เทียบ)")
p.add_argument("--key", help="key อะไรก็ได้ (ไม่ใส่ = เข้าด้วย --name)")
p.add_argument("--name", default="ทีมเรา (agent)", help="ชื่อทีมบนจออันดับ · พิมพ์ชื่อเดียวกันในหน้าเว็บ = ดูทีมเดียวกัน")
p.add_argument("--university", default="")
p.add_argument("--rules", action="store_true", help="ไม่ใช้ LLM")
p.add_argument("--ollama", action="store_true", help="ใช้ Ollama ที่ localhost:11434 แทน LLM ปลอม")
p.add_argument("--thinking", action="store_true")
p.add_argument("--forever", action="store_true")
a = p.parse_args()

base = (a.url or f"http://localhost:{a.port}").rstrip("/")
if not a.key:                                   # เข้าตลาดด้วยชื่อทีม (ไม่ต้องมี key) · รอตลาดเปิดก่อนถ้ายังไม่พร้อม
    import json
    import time
    import urllib.request
    for i in range(30):
        try:
            req = urllib.request.Request(base + "/v1/join", method="POST", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"team_name": a.name, "university": a.university}).encode())
            a.key = json.loads(urllib.request.urlopen(req, timeout=5).read())["key"]
            break
        except Exception as e:      # noqa: BLE001
            if i == 29:
                sys.exit(f"ต่อตลาดจำลองที่ {base} ไม่ได้: {e} (รัน mock-market/server.py แล้วหรือยัง?)")
            time.sleep(1)
os.environ.update({
    "MARKET_URL": base, "TEAM_KEY": a.key,
    "USE_LLM": "0" if a.rules else "1",
    "LLM_BASE_URL": "http://localhost:11434/v1" if a.ollama else f"{base}/llm/v1",
    "LLM_MODEL": os.environ.get("OLLAMA_MODEL", "qwen3.5:4b") if a.ollama else "event-llm",
    "LLM_API_KEY": "none" if a.ollama else a.key,
    "LLM_THINKING": "1" if a.thinking else "0",
})
os.environ.setdefault("LLM_TIMEOUT", "60")

sys.path.insert(0, str(STARTER))
os.chdir(STARTER)
import main  # noqa: E402  (main.py ของทีม)

if a.strategy:
    import importlib
    sys.path.insert(0, str(HERE / "strategies"))
    main.strategy = importlib.import_module(a.strategy)
    print("ใช้กลยุทธ์", a.strategy)
main.LOGS = HERE / "agent-logs" / a.key.split("-")[1] if a.key.count("-") >= 2 else HERE / "agent-logs"
main.LOGS.mkdir(parents=True, exist_ok=True)
print(f"agent → {base} · key {a.key} · LLM {os.environ['LLM_BASE_URL'] if os.environ['USE_LLM'] == '1' else 'ปิด'} · log {main.LOGS}")
if a.forever:
    sys.argv.append("--forever")
main.main()
