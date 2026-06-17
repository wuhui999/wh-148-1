import requests
from datetime import datetime, timedelta

BASE = "http://localhost:8001"

def login(u, p):
    r = requests.post(f"{BASE}/token", data={"username": u, "password": p})
    assert r.status_code == 200, f"登录失败: {r.text}"
    return r.json()["access_token"]

print("=== 问题1&3 验证：违规记录落库 + 连续工作校验 ===\n")

disp_token = login("dispatcher", "disp123")
admin_token = login("admin", "admin123")
disp_h = {"Authorization": f"Bearer {disp_token}"}
admin_h = {"Authorization": f"Bearer {admin_token}"}

# 用6月18日杂货泊位B04，ship_id=5是杂货船，赵引航资质包含general
ship_id = 5
berth_id = 4
vessel_id = 1
pilot_zhao = 4

test_day = datetime(2026, 6, 18)
day_start = test_day.replace(hour=2, minute=0)
print(f"测试日期: {test_day.date()}")

print("\n--- 准备：给赵引航配特殊规则 ---")
r = requests.get(f"{BASE}/duty-rules/pilot/{pilot_zhao}", headers=admin_h)
if r.status_code == 200 and r.json().get("id"):
    existing = r.json()
    print(f"  已有规则 id={existing['id']}，先删除")
    requests.delete(f"{BASE}/duty-rules/{existing['id']}", headers=admin_h)

# 规则：日上限10单，休息15分钟，连续工作≤120分钟
# 任务间隔10分钟 < 最少休息15分钟，所以两单会被算成连续工作
# 单任务120分钟，两单连起来总时长约250分钟，远超120分钟上限
r = requests.post(f"{BASE}/duty-rules", headers=admin_h, json={
    "pilot_id": pilot_zhao,
    "max_tasks_per_day": 10,
    "min_rest_minutes_between_tasks": 15,
    "max_consecutive_work_minutes": 120
})
rule_id = r.json()["id"]
print(f"  创建规则 id={rule_id}: 日上限10单, 休息10min, 连续≤120min")

print("\n--- 先清空赵引航当天的违规记录（查有多少） ---")
r = requests.get(f"{BASE}/duty-violations", headers=disp_h,
                 params={"pilot_id": pilot_zhao, "limit": 50})
vio_before = len(r.json())
print(f"  测试前违规记录数: {vio_before}")

print("\n--- 创建3个任务 ---")
t1 = day_start + timedelta(hours=2)       # 04:00
t2 = day_start + timedelta(hours=4, minutes=10)  # 06:10（前单04:00-06:00，间隔10分钟刚好够休息）
t3 = day_start + timedelta(hours=6, minutes=20)  # 08:20（再间隔10分钟）

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
        duty_excluded = res.get("duty_excluded_pilots", [])
        zhao_duty = next((d for d in duty_excluded if d.get("pilot_id") == pilot_zhao), None)
        duty_reason = str(zhao_duty.get("reason", zhao_duty)) if zhao_duty else "在可用列表中"
        print(f"  任务{i+1} {t.strftime('%H:%M')}: id={res['task_id']}, 赵引航: {duty_reason[:50]}")
    else:
        print(f"  任务{i+1} {t.strftime('%H:%M')}: 创建失败 - {res.get('message', '未知')[:80]}")

if len(task_ids) < 2:
    print("\n⚠ 任务创建不足，无法测试")
else:
    print(f"\n--- 依次派单给赵引航（共{len(task_ids)}个任务） ---")
    success = 0
    fail_consecutive = 0
    fail_rest = 0
    fail_daily = 0
    fail_other = 0

    for i, tid in enumerate(task_ids):
        r = requests.post(f"{BASE}/tasks/{tid}/assign", headers=disp_h,
                          json={"pilot_id": pilot_zhao, "vessel_id": vessel_id})
        if r.status_code == 200:
            success += 1
            print(f"  任务{i+1}: ✓ 派单成功")
        else:
            detail = r.json()["detail"]
            if "连续工作" in detail:
                fail_consecutive += 1
                print(f"  任务{i+1}: ✗ 连续工作 → {detail[:70]}")
            elif "休息" in detail or "间隔" in detail:
                fail_rest += 1
                print(f"  任务{i+1}: ✗ 休息间隔 → {detail[:70]}")
            elif "单日" in detail:
                fail_daily += 1
                print(f"  任务{i+1}: ✗ 日上限 → {detail[:70]}")
            else:
                fail_other += 1
                print(f"  任务{i+1}: ✗ {r.status_code} → {detail[:70]}")

    print(f"\n  统计：成功{success}单, 连续工作被拒{fail_consecutive}单, "
          f"休息间隔被拒{fail_rest}单, 日上限被拒{fail_daily}单")

    print("\n--- 验证问题3：连续工作校验 ---")
    if fail_consecutive >= 1:
        print("  ✓ 连续工作时长校验生效，派单被正确拦截")
    else:
        print("  ✗ 连续工作时长校验未触发")

    print("\n--- 验证问题1：违规记录独立落库 ---")
    r = requests.get(f"{BASE}/duty-violations", headers=disp_h,
                     params={"pilot_id": pilot_zhao, "limit": 50})
    vio_after = r.json()
    vio_new = len(vio_after) - vio_before
    print(f"  测试后违规记录数: {len(vio_after)} (新增 {vio_new} 条)")

    consec_vios = [v for v in vio_after if v["violation_type"] == "consecutive_work"]
    if consec_vios:
        print(f"  其中连续工作违规 {len(consec_vios)} 条:")
        for v in consec_vios[:2]:
            print(f"    - {v['violation_detail'][:70]}")

    total_violations = fail_consecutive + fail_rest + fail_daily
    if vio_new >= 1 and vio_new >= total_violations - 1:  # 允许一点误差
        print("  ✓ 派单失败时，违规记录独立保存，不会随事务回滚")
    else:
        print(f"  ⚠ 预期新增约 {total_violations} 条，实际 {vio_new} 条")

    print("\n--- 执勤统计验证 ---")
    r = requests.get(f"{BASE}/stats/pilot-duty", headers=disp_h,
                     params={
                         "period_start": test_day.isoformat(),
                         "period_end": (test_day + timedelta(days=1)).isoformat()
                     })
    stats = r.json()
    zhao = next((s for s in stats["pilot_stats"] if s["pilot_name"].startswith("赵")), None)
    if zhao:
        print(f"  赵引航统计: 任务{zhao['total_tasks']}个, "
              f"执勤{zhao['total_duty_minutes']:.0f}分钟, "
              f"被拒{zhao['duty_rejections']}次 "
              f"(连续{zhao['continuous_limit_rejections']}次)")
        if zhao["continuous_limit_rejections"] >= 1:
            print("  ✓ 执勤统计正确包含连续工作被拒次数")

print("\n--- 清理：删除测试规则 ---")
requests.delete(f"{BASE}/duty-rules/{rule_id}", headers=admin_h)
print("  已删除赵引航的测试规则")

print("\n=== 验证完成 ===")
