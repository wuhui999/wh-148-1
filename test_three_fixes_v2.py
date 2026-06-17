import requests
from datetime import datetime, timedelta

BASE = "http://localhost:8001"

def login(u, p):
    r = requests.post(f"{BASE}/token", data={"username": u, "password": p})
    assert r.status_code == 200, f"登录失败: {r.text}"
    return r.json()["access_token"]

print("=== 执勤约束三大问题修复验证 v2 ===\n")

disp_token = login("dispatcher", "disp123")
admin_token = login("admin", "admin123")
disp_h = {"Authorization": f"Bearer {disp_token}"}
admin_h = {"Authorization": f"Bearer {admin_token}"}

ship_id = 5
berth_id = 4
vessel_id = 1
pilot_zhao = 4  # 赵引航，用他来测试，临时配规则

# 找一个未来日期，确保之前没有任务
test_day = datetime(2026, 6, 25)
day_start = test_day.replace(hour=2, minute=0)
print(f"测试日期: {test_day.date()}\n")

print("========== 准备：给赵引航配一条特殊规则 ==========")
r = requests.get(f"{BASE}/duty-rules/pilot/{pilot_zhao}", headers=admin_h)
if r.status_code == 200:
    existing = r.json()
    print(f"  已有规则，先删除...")
    requests.delete(f"{BASE}/duty-rules/{existing['id']}", headers=admin_h)

# 配规则：日上限10单（很高），最少休息10分钟（很短），连续工作最长120分钟（2小时，很短）
r = requests.post(f"{BASE}/duty-rules", headers=admin_h, json={
    "pilot_id": pilot_zhao,
    "max_tasks_per_day": 10,
    "min_rest_minutes_between_tasks": 10,
    "max_consecutive_work_minutes": 120
})
rule_id = r.json()["id"]
print(f"  创建规则 id={rule_id}: 日上限10单, 休息10分钟, 连续工作≤120分钟")

print("\n========== 问题2：规则变更审计查询 ==========")
r = requests.get(f"{BASE}/duty-rules/audit-logs", headers=admin_h,
                 params={"pilot_id": pilot_zhao, "limit": 10})
logs = r.json()
print(f"  赵引航规则审计记录: {len(logs)} 条")
for log in logs[:3]:
    print(f"    - [{log['action']}] {log['remark']}")
if len(logs) >= 1 and logs[0]["action"] == "duty_rule_created":
    print("  ✓ 规则创建审计正常记录，可独立查询")
else:
    print("  ✗ 规则审计有问题")

print("\n========== 问题3：连续工作时长校验 ==========")
print("  赵引航规则：连续工作≤120分钟，单任务默认120分钟")
print("  任务1（2小时）→ 休息10分钟 → 任务2，总连续时间将 > 120分钟")
print("  预期：任务2因连续工作超时而被拒")

t1 = day_start + timedelta(hours=1)      # 03:00
t2 = day_start + timedelta(hours=3, minutes=10)  # 05:10，间隔10分钟（刚好够休息）
t3 = day_start + timedelta(hours=5, minutes=20)  # 07:20，间隔10分钟

task_ids = []
for i, t in enumerate([t1, t2, t3]):
    r = requests.post(f"{BASE}/tasks", headers=disp_h, json={
        "ship_id": ship_id, "berth_id": berth_id, "task_type": "boarding",
        "planned_boarding_time": t.isoformat(),
        "notes": f"连续工作测试-{i+1}"
    })
    res = r.json()
    if res.get("task_id"):
        task_ids.append(res["task_id"])
        print(f"  任务{i+1}: {t.strftime('%H:%M')} 创建, id={res['task_id']}")
    else:
        print(f"  任务{i+1}: 创建失败 - {res}")

print(f"\n  依次派给赵引航：")
success = 0
fail_consecutive = 0
fail_daily = 0
fail_rest = 0
for i, tid in enumerate(task_ids):
    r = requests.post(f"{BASE}/tasks/{tid}/assign", headers=disp_h,
                      json={"pilot_id": pilot_zhao, "vessel_id": vessel_id})
    if r.status_code == 200:
        success += 1
        print(f"    任务{i+1}: ✓ 派单成功")
    else:
        detail = r.json()["detail"]
        if "连续工作" in detail:
            fail_consecutive += 1
            print(f"    任务{i+1}: ✗ 连续工作 - {detail[:70]}")
        elif "单日" in detail:
            fail_daily += 1
            print(f"    任务{i+1}: ✗ 日上限 - {detail[:70]}")
        elif "休息" in detail or "间隔" in detail:
            fail_rest += 1
            print(f"    任务{i+1}: ✗ 休息间隔 - {detail[:70]}")
        else:
            print(f"    任务{i+1}: ✗ {r.status_code} - {detail[:70]}")

print(f"\n  结果：成功{success}单, 连续工作被拒{fail_consecutive}单, 日上限被拒{fail_daily}单, 休息间隔被拒{fail_rest}单")
if fail_consecutive >= 1:
    print("  ✓ 连续工作时长校验生效")
else:
    print("  ✗ 连续工作时长校验未触发")

print("\n========== 问题1：违规记录独立落库 ==========")
r = requests.get(f"{BASE}/duty-violations", headers=disp_h,
                 params={"pilot_id": pilot_zhao, "limit": 20})
violations = r.json()
print(f"  赵引航违规记录总数: {len(violations)} 条")

consec_count = sum(1 for v in violations if v["violation_type"] == "consecutive_work")
daily_count = sum(1 for v in violations if v["violation_type"] == "daily_task_limit")
rest_count = sum(1 for v in violations if v["violation_type"] == "rest_interval")
print(f"    日上限: {daily_count}, 休息间隔: {rest_count}, 连续工作: {consec_count}")

if consec_count >= 1:
    print("  ✓ 派单失败时，违规记录独立保存，不会随事务回滚")
else:
    print("  ⚠ 没有连续工作违规记录（可能没触发连续工作校验）")

print("\n========== 执勤统计接口验证 ==========")
r = requests.get(f"{BASE}/stats/pilot-duty", headers=disp_h,
                 params={
                     "period_start": (test_day - timedelta(days=1)).isoformat(),
                     "period_end": (test_day + timedelta(days=1)).isoformat()
                 })
stats = r.json()
zhao_stat = next((s for s in stats["pilot_stats"] if s["pilot_name"].startswith("赵")), None)
if zhao_stat:
    print(f"  赵引航: 任务数{zhao_stat['total_tasks']}, "
          f"被拒{zhao_stat['duty_rejections']}次 "
          f"(连续{zhao_stat['continuous_limit_rejections']}次)")
    if zhao_stat["continuous_limit_rejections"] >= 1:
        print("  ✓ 执勤统计正确统计连续工作被拒次数")

print("\n========== 再验证：修改规则也有审计 ==========")
r = requests.put(f"{BASE}/duty-rules/{rule_id}", headers=admin_h,
                 json={"max_consecutive_work_minutes": 180})
print(f"  修改规则连续工作时长为180分钟 → 状态 {r.status_code}")

r = requests.get(f"{BASE}/duty-rules/audit-logs", headers=admin_h,
                 params={"pilot_id": pilot_zhao, "limit": 10})
logs = r.json()
update_log = next((l for l in logs if l["action"] == "duty_rule_updated"), None)
if update_log:
    print(f"  ✓ 规则更新审计已记录: {update_log['old_value']} → {update_log['new_value']}")
else:
    print("  ✗ 规则更新没有审计记录")

print("\n========== 清理：删除测试规则 ==========")
requests.delete(f"{BASE}/duty-rules/{rule_id}", headers=admin_h)
print("  已删除测试规则")

print("\n========== 全部验证完成 ==========")
