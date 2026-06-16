from datetime import datetime, timedelta
from database import engine, SessionLocal
from models import (
    Base, User, Pilot, Berth, Ship, TideWindow, SafetyMarginConfig,
    TransportVessel, UserRole, ShipType, PilotQualification, VesselStatus
)
from security import get_password_hash

Base.metadata.create_all(bind=engine)
db = SessionLocal()

try:
    print("开始初始化种子数据...")

    if not db.query(User).filter(User.username == "admin").first():
        db.add_all([
            User(
                username="admin",
                hashed_password=get_password_hash("admin123"),
                full_name="系统管理员",
                role=UserRole.ADMIN,
                is_active=True
            ),
            User(
                username="dispatcher",
                hashed_password=get_password_hash("disp123"),
                full_name="调度员张三",
                role=UserRole.DISPATCHER,
                is_active=True
            ),
            User(
                username="dispatcher2",
                hashed_password=get_password_hash("disp456"),
                full_name="调度员李四",
                role=UserRole.DISPATCHER,
                is_active=True
            )
        ])
        print("✓ 用户数据已创建 (admin/admin123, dispatcher/disp123)")
    else:
        print("- 用户数据已存在，跳过")

    if not db.query(Pilot).first():
        db.add_all([
            Pilot(
                license_number="PLT-2024-001",
                name="王引航",
                phone="13800138001",
                qualification=PilotQualification.MASTER,
                certified_ship_types="container,bulk,tanker,passenger,general,roro",
                is_active=True
            ),
            Pilot(
                license_number="PLT-2024-002",
                name="李引航",
                phone="13800138002",
                qualification=PilotQualification.SENIOR,
                certified_ship_types="container,bulk,general,roro",
                is_active=True
            ),
            Pilot(
                license_number="PLT-2024-003",
                name="张引航",
                phone="13800138003",
                qualification=PilotQualification.SENIOR,
                certified_ship_types="tanker,passenger,general",
                is_active=True
            ),
            Pilot(
                license_number="PLT-2024-004",
                name="赵引航",
                phone="13800138004",
                qualification=PilotQualification.JUNIOR,
                certified_ship_types="general,container",
                is_active=True
            )
        ])
        print("✓ 引航员数据已创建 (4名，各级资质)")
    else:
        print("- 引航员数据已存在，跳过")

    if not db.query(Berth).first():
        db.add_all([
            Berth(code="B01", name="1号集装箱泊位", max_draft=14.5, max_length=350),
            Berth(code="B02", name="2号散货泊位", max_draft=13.0, max_length=300),
            Berth(code="B03", name="3号油轮泊位", max_draft=15.0, max_length=330),
            Berth(code="B04", name="4号杂货泊位", max_draft=10.0, max_length=200),
            Berth(code="B05", name="5号客轮泊位", max_draft=9.5, max_length=250)
        ])
        print("✓ 泊位数据已创建 (5个泊位)")
    else:
        print("- 泊位数据已存在，跳过")

    if not db.query(Ship).first():
        db.add_all([
            Ship(
                imo_number="IMO9700001",
                name="远洋一号",
                ship_type=ShipType.CONTAINER,
                draft=12.5,
                length=300,
                flag="PANAMA"
            ),
            Ship(
                imo_number="IMO9700002",
                name="散运之星",
                ship_type=ShipType.BULK,
                draft=11.0,
                length=280,
                flag="LIBERIA"
            ),
            Ship(
                imo_number="IMO9700003",
                name="蓝鲸号油轮",
                ship_type=ShipType.TANKER,
                draft=14.0,
                length=320,
                flag="SINGAPORE"
            ),
            Ship(
                imo_number="IMO9700004",
                name="和平客轮",
                ship_type=ShipType.PASSENGER,
                draft=8.5,
                length=220,
                flag="CHINA"
            ),
            Ship(
                imo_number="IMO9700005",
                name="杂货先锋",
                ship_type=ShipType.GENERAL,
                draft=9.0,
                length=180,
                flag="CHINA"
            ),
            Ship(
                imo_number="IMO9700006",
                name="滚装一号",
                ship_type=ShipType.RORO,
                draft=9.5,
                length=200,
                flag="JAPAN"
            )
        ])
        print("✓ 船舶数据已创建 (6艘不同类型船舶)")
    else:
        print("- 船舶数据已存在，跳过")

    if not db.query(TideWindow).first():
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        tide_data = []

        for day_offset in range(7):
            day_base = now + timedelta(days=day_offset)

            tide_data.append(TideWindow(
                berth_id=1,
                window_start=day_base.replace(hour=4),
                window_end=day_base.replace(hour=10),
                min_safe_draft=14.0,
                tide_height=2.5
            ))
            tide_data.append(TideWindow(
                berth_id=1,
                window_start=day_base.replace(hour=16),
                window_end=day_base.replace(hour=22),
                min_safe_draft=14.2,
                tide_height=2.8
            ))

            tide_data.append(TideWindow(
                berth_id=2,
                window_start=day_base.replace(hour=3),
                window_end=day_base.replace(hour=9),
                min_safe_draft=12.5,
                tide_height=2.2
            ))
            tide_data.append(TideWindow(
                berth_id=2,
                window_start=day_base.replace(hour=15),
                window_end=day_base.replace(hour=21),
                min_safe_draft=12.8,
                tide_height=2.5
            ))

            tide_data.append(TideWindow(
                berth_id=3,
                window_start=day_base.replace(hour=5),
                window_end=day_base.replace(hour=11),
                min_safe_draft=14.5,
                tide_height=3.0
            ))
            tide_data.append(TideWindow(
                berth_id=3,
                window_start=day_base.replace(hour=17),
                window_end=day_base.replace(hour=23),
                min_safe_draft=14.8,
                tide_height=3.2
            ))

            tide_data.append(TideWindow(
                berth_id=4,
                window_start=day_base.replace(hour=2),
                window_end=day_base.replace(hour=14),
                min_safe_draft=9.5,
                tide_height=1.8
            ))

            tide_data.append(TideWindow(
                berth_id=5,
                window_start=day_base.replace(hour=6),
                window_end=day_base.replace(hour=18),
                min_safe_draft=9.0,
                tide_height=1.5
            ))

        db.add_all(tide_data)
        print(f"✓ 潮汐窗口数据已创建 ({len(tide_data)}个未来7天的窗口)")
    else:
        print("- 潮汐窗口数据已存在，跳过")

    if not db.query(SafetyMarginConfig).first():
        db.add_all([
            SafetyMarginConfig(ship_type=ShipType.CONTAINER, draft_margin=0.3, time_margin_minutes=30),
            SafetyMarginConfig(ship_type=ShipType.BULK, draft_margin=0.4, time_margin_minutes=45),
            SafetyMarginConfig(ship_type=ShipType.TANKER, draft_margin=0.5, time_margin_minutes=60),
            SafetyMarginConfig(ship_type=ShipType.PASSENGER, draft_margin=0.2, time_margin_minutes=30),
            SafetyMarginConfig(ship_type=ShipType.GENERAL, draft_margin=0.3, time_margin_minutes=30),
            SafetyMarginConfig(ship_type=ShipType.RORO, draft_margin=0.3, time_margin_minutes=30)
        ])
        print("✓ 安全余量配置已创建 (6种船舶类型)")
    else:
        print("- 安全余量配置已存在，跳过")

    if not db.query(TransportVessel).first():
        now = datetime.now()
        db.add_all([
            TransportVessel(
                name="交通艇01号",
                capacity=8,
                status=VesselStatus.AVAILABLE,
                available_from=now.replace(hour=0, minute=0, second=0),
                available_to=now.replace(hour=23, minute=59, second=0) + timedelta(days=7),
                maintenance_notes=None
            ),
            TransportVessel(
                name="交通艇02号",
                capacity=12,
                status=VesselStatus.AVAILABLE,
                available_from=now.replace(hour=0, minute=0, second=0),
                available_to=now.replace(hour=23, minute=59, second=0) + timedelta(days=7),
                maintenance_notes=None
            ),
            TransportVessel(
                name="交通艇03号",
                capacity=6,
                status=VesselStatus.IN_MAINTENANCE,
                available_from=None,
                available_to=None,
                maintenance_notes="发动机定期保养，预计3天后恢复"
            ),
            TransportVessel(
                name="交通艇04号",
                capacity=10,
                status=VesselStatus.AVAILABLE,
                available_from=now.replace(hour=0, minute=0, second=0),
                available_to=now.replace(hour=23, minute=59, second=0) + timedelta(days=7),
                maintenance_notes=None
            )
        ])
        print("✓ 交通艇数据已创建 (4艘，其中1艘维护中)")
    else:
        print("- 交通艇数据已存在，跳过")

    db.commit()
    print("\n种子数据初始化完成！")
    print("\n默认登录账号：")
    print("  管理员: admin / admin123")
    print("  调度员: dispatcher / disp123")
    print("\n启动服务: python main.py  (端口 8001)")
    print("API文档: http://localhost:8001/docs")

except Exception as e:
    db.rollback()
    print(f"错误: {e}")
    import traceback
    traceback.print_exc()
finally:
    db.close()
