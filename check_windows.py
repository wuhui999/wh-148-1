import requests
from datetime import datetime, timedelta

BASE = "http://localhost:8001"

r = requests.post(f"{BASE}/token", data={"username": "dispatcher", "password": "disp123"})
token = r.json()["access_token"]
h = {"Authorization": f"Bearer {token}"}

print("=== 1号集装箱泊位 潮汐窗口 ===")
r = requests.get(f"{BASE}/tide-windows", headers=h, params={"berth_id": 1})
windows = r.json()
for w in windows[:6]:
    print(f"  id={w['id']}  {w['window_start']} ~ {w['window_end']}  最小吃水={w['min_safe_draft']}m")

print("\n=== 查询未来2天内可用的窗口 ===")
now = datetime.now()
r = requests.get(
    f"{BASE}/tide-windows",
    headers=h,
    params={
        "berth_id": 1,
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(days=2)).isoformat()
    }
)
windows = r.json()
print(f"找到 {len(windows)} 个窗口")
for w in windows[:4]:
    print(f"  {w['window_start']} ~ {w['window_end']}  draft={w['min_safe_draft']}m")

if windows:
    w = windows[0]
    start = datetime.fromisoformat(w['window_start'].replace('Z', '+00:00'))
    boarding_time = start + timedelta(hours=1, minutes=30)
    print(f"\n建议测试时间: {boarding_time.isoformat()}")
    print(f"  (窗口开始+1.5小时，预留30分钟安全余量)")

print("\n=== 现有任务 ===")
r = requests.get(f"{BASE}/tasks", headers=h, params={"limit": 5})
tasks = r.json()
print(f"共 {len(tasks)} 个任务")
for t in tasks:
    print(f"  {t['task_number']}  pilot={t['pilot_id']}  status={t['status']}  time={t['planned_boarding_time']}")
