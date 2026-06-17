import requests
from datetime import datetime, timedelta

BASE = "http://localhost:8001"

def login(u, p):
    r = requests.post(f"{BASE}/token", data={"username": u, "password": p})
    assert r.status_code == 200, f"登录失败: {r.text}"
    return r.json()["access_token"]

print("=== 执勤约束三大问题修复验证 ===\n")

disp_token = login("dispatcher", "disp123")
admin_token = login("admin", "admin123")
disp_h = {"Authorization": f"Bearer {disp_token}"}
admin_h = {"Authorization": f"Bearer {admin_token}"}

ship_id = 5
berth_id = 4
vessel_id = 1
pilot_li = 2   # 李引航，个人规则：日2单，休息120分钟，连续240分钟
pilot_wang = 1 # 王引航，全局规则：日5单，休息60分钟，连续480分钟

window_day = datetime(2026, 6, 18)
window_start = window_day.replace(hour=2, minute=0)
print(f"测试日期: {window_day.date()}")

print("\n========== 问题1：违规记录独立落库 ==========")
r = requests.get(f"{BASE}/duty-violations", headers=disp_h,
                 params={"pilot_id": pilot_wang, "limit": 20})
wang_before = len(r.json())
print(f"测试前王引航违规记录数: {wang_before}")

t1 = window_start + timedelta(hours=1)
t2 = window_start + timedelta(hours=2, minutes=30)

r = requests.post(f"{BASE}/tasks", headers=disp_h, json={
    "ship_id": ship_id, "berth_id": berth_id, "task_type": "boarding",
    "planned_boarding_time": t1.isoformat(),
    "notes": "违规记录测试-A"
})
tid_a = r.json()["task_id"]
requests.post(f"{BASE}/tasks/{tid_a}/assign", headers=disp_h,
              json={"pilot_id": pilot_wang, "vessel_id": vessel_id})
print(f"  任务A: {t1.strftime('%H:%M')} - 派给王引航 ✓")

r = requests.post(f"{BASE}/tasks", headers=disp_h, json={
    "ship_id": ship_id, "berth_id": berth_id, "task_type": "boarding",
    "planned_boarding_time": t2.isoformat(),
    "notes": "违规记录测试-B"
})
tid_b = r.json()["task_id"]

r = requests.post(f"{BASE}/tasks/{tid_b}/assign", headers=disp_h,
                  json={"pilot_id": pilot_wang, "vessel_id": vessel_id})
status = r.status_code
reason = r.json()["detail"] if status != 200 else "成功"
print(f"  任务B: {t2.strftime('%H:%M')} - 派给王引航 → {status} {reason[:60]}")

r = requests.get(f"{BASE}/duty-violations", headers=disp_h,
                 params={"pilot_id": pilot_wang, "limit": 20})
wang_after = len(r.json())
new_count = wang_after - wang_before
print(f"\n  测试后违规记录数: {wang_after} (新增 {new_count} 条)")
if new_count >= 1:
    print("  ✓ 违规记录独立落库，不会随派单事务回滚")
else:
    print("  ✗ 违规记录丢失了")

print("\n========== 问题2：规则变更审计独立查询 ==========")
r = requests.get(f"{BASE}/duty-rules/audit-logs", headers=admin_h,
                 params={"limit": 10})
logs = r.json()
print(f"  规则审计记录总数: {len(logs)} 条")
if len(logs) > 0:
    print("  最近3条:")
    for log in logs[:3]:
        who = "全局" if log["is_global"] else f"引航员{log['pilot_id']}"
        print(f"    - [{log['action']}] {who} - {log['remark']}")
    print("  ✓ 规则变更审计可以独立查询，不依赖假 task_id")
else:
    print("  没有审计记录，先修改一条规则产生一条...")
    r = requests.put(f"{BASE}/duty-rules/1", headers=admin_h,
                     json={"max_tasks_per_day": 6})
    print(f"  修改全局规则 → 状态 {r.status_code}")
    r = requests.get(f"{BASE}/duty-rules/audit-logs", headers=admin_h,
                     params={"limit": 5})
    logs = r.json()
    print(f"  修改后审计记录数: {len(logs)}")
    if len(logs) > 0:
        print("  ✓ 规则变更审计正常记录并可查询")
    else:
        print("  ✗ 审计记录有问题")

print("\n========== 问题3：连续工作时长校验 ==========")
print("  李引航个人规则：连续工作≤240分钟（4小时），休息≥120分钟")
print("  安排3个短任务，间隔都很小，看会不会触发连续工作限制")

t_li_1 = window_start + timedelta(hours=6)
t_li_2 = window_start + timedelta(hours=8, minutes=30)  # 间隔30分钟，不够休息
t_li_3 = window_start + timedelta(hours=11)             # 又间隔30分钟

# 先确认李引航当天有没有任务，清空状态
r = requests.get(f"{BASE}/duty-violations", headers=disp_h,
                 params={"pilot_id": pilot_li, "limit": 20})
li_before = len(r.json())

task_ids_li = []
for i, t in enumerate([t_li_1, t_li_2, t_li_3]):
    r = requests.post(f"{BASE}/tasks", headers=disp_h, json={
        "ship_id": ship_id, "berth_id": berth_id, "task_type": "boarding",
        "planned_boarding_time": t.isoformat(),
        "notes": f"连续工作测试-{i+1}"
    })
    tid = r.json()["task_id"]
    task_ids_li.append(tid)
    print(f"  任务{i+1}: {t.strftime('%H:%M')} - id={tid}")

print("\n  依次派给李引航：")
success = 0
fail_consecutive = 0
for i, tid in enumerate(task_ids_li):
    r = requests.post(f"{BASE}/tasks/{tid}/assign", headers=disp_h,
                      json={"pilot_id": pilot_li, "vessel_id": vessel_id})
    if r.status_code == 200:
        success += 1
        print(f"    任务{i+1}: ✓ 成功")
    else:
        detail = r.json()["detail"]
        is_consecutive = "连续工作" in detail
        if is_consecutive:
            fail_consecutive += 1
        print(f"    任务{i+1}: ✗ {detail[:80]}")

print(f"\n  成功 {success} 单, 因连续工作被拒 {fail_consecutive} 单")
if fail_consecutive >= 1:
    print("  ✓ 连续工作时长校验生效")
else:
    print("  ⚠ 没有触发连续工作限制（可能日上限先拦住了）")

r = requests.get(f"{BASE}/duty-violations", headers=disp_h,
                 params={"pilot_id": pilot_li, "limit": 20})
li_violations = r.json()
print(f"\n  李引航违规记录（{len(li_violations)} 条）:")
consec_count = sum(1 for v in li_violations if v["violation_type"] == "consecutive_work")
daily_count = sum(1 for v in li_violations if v["violation_type"] == "daily_task_limit")
rest_count = sum(1 for v in li_violations if v["violation_type"] == "rest_interval")
print(f"    日上限: {daily_count} 次, 休息间隔: {rest_count} 次, 连续工作: {consec_count} 次")

print("\n========== 执勤统计验证 ==========")
r = requests.get(f"{BASE}/stats/pilot-duty", headers=disp_h,
                 params={
                     "period_start": (window_day - timedelta(days=1)).isoformat(),
                     "period_end": (window_day + timedelta(days=1)).isoformat()
                 })
stats = r.json()
print(f"  统计周期内引航员数: {len(stats['pilot_stats'])}")
for s in stats["pilot_stats"]:
    if s["duty_rejections"] > 0:
        print(f"    {s['pilot_name']}: 被拒 {s['duty_rejections']} 次 "
              f"(日上限{s['daily_limit_rejections']}, "
              f"休息{s['rest_interval_rejections']}, "
              f"连续{s['continuous_limit_rejections']})")

print("\n========== 全部验证完成 ==========")
