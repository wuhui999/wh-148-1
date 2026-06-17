import requests
from datetime import datetime, timedelta

BASE = "http://localhost:8001"

def login(u, p):
    r = requests.post(f"{BASE}/token", data={"username": u, "password": p})
    return r.json()["access_token"]

token = login("dispatcher", "disp123")
h = {"Authorization": f"Bearer {token}"}

print("=== 执勤违规记录保存验证 ===\n")

ship_id = 5
berth_id = 4
vessel_id = 1
pilot_li = 2

window_day = datetime(2026, 6, 18)
window_start = window_day.replace(hour=2, minute=0)
print(f"测试日期: {window_day.date()}")

r = requests.get(f"{BASE}/duty-violations", headers=h,
                 params={"pilot_id": pilot_li, "limit": 20})
before = len(r.json())
print(f"测试前李引航违规记录数: {before}")

print("\n--- 创建3个任务，尝试都派给李引航（他个人规则日上限2单）---")

task_ids = []
for i in range(3):
    t = window_start + timedelta(hours=1 + i * 4)
    r = requests.post(f"{BASE}/tasks", headers=h, json={
        "ship_id": ship_id, "berth_id": berth_id, "task_type": "boarding",
        "planned_boarding_time": t.isoformat(),
        "boarding_point": f"锚地{i+1}",
        "notes": f"违规记录测试-{i+1}"
    })
    res = r.json()
    if res.get("task_id"):
        task_ids.append(res["task_id"])
        print(f"  任务{i+1} 创建: {t.strftime('%H:%M')}, id={res['task_id']}")

print(f"\n--- 依次派单给李引航 ---")
ok = 0
fail = 0
for i, tid in enumerate(task_ids):
    r = requests.post(f"{BASE}/tasks/{tid}/assign", headers=h,
                      json={"pilot_id": pilot_li, "vessel_id": vessel_id})
    if r.status_code == 200:
        ok += 1
        print(f"  任务{i+1}: ✓ 派单成功")
    else:
        fail += 1
        err = r.json()["detail"]
        is_duty = "执勤" in err
        print(f"  任务{i+1}: ✗ 被拒 ({'执勤规则' if is_duty else '其他'}) - {err[:60]}...")

print(f"\n结果: 成功 {ok} 单, 失败 {fail} 单")
print(f"预期: 成功 2 单, 失败 1 单 (李引航日上限 2 单)")

r = requests.get(f"{BASE}/duty-violations", headers=h,
                 params={"pilot_id": pilot_li, "limit": 20})
after = len(r.json())
new_violations = after - before
print(f"\n测试后李引航违规记录数: {after} (新增 {new_violations} 条)")

if new_violations > 0:
    print("✓ 违规记录成功保存到数据库")
    vios = r.json()
    print("  最近2条:")
    for v in vios[:2]:
        print(f"    - {v['violation_type']}: {v['violation_detail'][:50]}")
else:
    print("✗ 违规记录没有保存")

print("\n=== 休息间隔测试 ===")
pilot_wang = 1
r = requests.get(f"{BASE}/duty-violations", headers=h,
                 params={"pilot_id": pilot_wang, "limit": 20})
wang_before = len(r.json())
print(f"测试前王引航违规记录数: {wang_before}")

t1 = window_start + timedelta(hours=1)
t2 = window_start + timedelta(hours=3, minutes=30)

r = requests.post(f"{BASE}/tasks", headers=h, json={
    "ship_id": ship_id, "berth_id": berth_id, "task_type": "boarding",
    "planned_boarding_time": t1.isoformat(),
    "notes": "王引航-间隔测试A"
})
tid_a = r.json()["task_id"]
requests.post(f"{BASE}/tasks/{tid_a}/assign", headers=h,
              json={"pilot_id": pilot_wang, "vessel_id": vessel_id})
print(f"  任务A: {t1.strftime('%H:%M')} - 派给王引航 ✓")

r = requests.post(f"{BASE}/tasks", headers=h, json={
    "ship_id": ship_id, "berth_id": berth_id, "task_type": "boarding",
    "planned_boarding_time": t2.isoformat(),
    "notes": "王引航-间隔测试B"
})
tid_b = r.json()["task_id"]
print(f"  任务B: {t2.strftime('%H:%M')} - 与任务A结束间隔30分钟")

r = requests.post(f"{BASE}/tasks/{tid_b}/assign", headers=h,
                  json={"pilot_id": pilot_wang, "vessel_id": vessel_id})
if r.status_code == 400 and "执勤" in r.json()["detail"]:
    print("  任务B派给王引航: ✗ 被执勤规则拒绝 ✓")
else:
    print(f"  任务B派给王引航: 状态 {r.status_code}")

r = requests.get(f"{BASE}/duty-violations", headers=h,
                 params={"pilot_id": pilot_wang, "limit": 20})
wang_after = len(r.json())
print(f"测试后王引航违规记录数: {wang_after} (新增 {wang_after - wang_before} 条)")
if wang_after - wang_before > 0:
    print("✓ 休息间隔违规记录已保存")
else:
    print("✗ 休息间隔违规记录没有保存")

print("\n=== 执勤统计验证 ===")
r = requests.get(f"{BASE}/stats/pilot-duty", headers=h,
                 params={
                     "period_start": (window_day - timedelta(days=1)).isoformat(),
                     "period_end": (window_day + timedelta(days=1)).isoformat()
                 })
stats = r.json()
print(f"统计周期内引航员数: {len(stats['pilot_stats'])}")
for s in stats["pilot_stats"]:
    if s["duty_rejections"] > 0:
        print(f"  {s['pilot_name']}: 被拒 {s['duty_rejections']} 次 "
              f"(日上限{s['daily_limit_rejections']}, 休息{s['rest_interval_rejections']})")

print("\n=== 全部验证完成 ===")
