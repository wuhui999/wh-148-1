from datetime import datetime, timedelta
from typing import List, Optional
from collections import Counter

from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import and_, or_, func
from sqlalchemy.orm import Session

from database import engine, Base, get_db
from models import (
    User, Pilot, Berth, Ship, TideWindow, SafetyMarginConfig,
    TransportVessel, PilotTask, AuditLog, UserRole, TaskStatus,
    ShipType, PilotQualification, VesselStatus, AuditAction,
    PilotDutyRule, DutyViolationLog, ViolationType, DutyRuleAuditLog
)
from schemas import (
    Token, UserCreate, UserResponse, PilotCreate, PilotResponse,
    PilotUpdate, BerthCreate, BerthResponse, ShipCreate, ShipResponse,
    ShipUpdate, TideWindowCreate, TideWindowResponse, TideWindowSimple,
    WindowQuery, SafetyMarginCreate, SafetyMarginResponse,
    TransportVesselCreate, TransportVesselResponse, TransportVesselUpdate,
    VesselAvailabilityQuery, PilotTaskCreate, PilotTaskAssign,
    PilotTaskStatusUpdate, PilotTaskDelay, PilotTaskCancel,
    PilotTaskResponse, TaskMatchResult, AuditLogResponse,
    WindowUtilizationStats, DelayStats,
    PilotDutyRuleCreate, PilotDutyRuleUpdate, PilotDutyRuleResponse,
    DutyViolationResponse, PilotDutyStatsResponse, PilotDutyStatsItem,
    DutyRuleAuditLogResponse
)
from security import (
    get_password_hash, authenticate_user, create_access_token,
    get_current_user, get_current_active_dispatcher, get_current_admin,
    ACCESS_TOKEN_EXPIRE_MINUTES
)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="引航任务调度系统", version="1.1.0")


def generate_task_number(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"PLT{today}"
    last_task = (
        db.query(PilotTask)
        .filter(PilotTask.task_number.like(f"{prefix}%"))
        .order_by(PilotTask.id.desc())
        .first()
    )
    if last_task:
        seq = int(last_task.task_number[-3:]) + 1
    else:
        seq = 1
    return f"{prefix}{seq:03d}"


def create_audit_log(
    db: Session, task_id: int, operator_id: int, action: AuditAction,
    old_value: Optional[str] = None, new_value: Optional[str] = None,
    remark: Optional[str] = None
):
    log = AuditLog(
        task_id=task_id,
        operator_id=operator_id,
        action=action,
        old_value=old_value,
        new_value=new_value,
        remark=remark
    )
    db.add(log)
    db.flush()


def get_safety_margin(db: Session, ship_type: ShipType) -> SafetyMarginConfig:
    margin = db.query(SafetyMarginConfig).filter(
        SafetyMarginConfig.ship_type == ship_type
    ).first()
    if not margin:
        margin = SafetyMarginConfig(
            ship_type=ship_type, draft_margin=0.3, time_margin_minutes=30
        )
    return margin


def match_tide_window(
    db: Session, berth_id: int, ship_draft: float, ship_type: ShipType,
    planned_time: datetime, task_duration_minutes: int = 120
):
    margin = get_safety_margin(db, ship_type)
    required_draft = ship_draft + margin.draft_margin
    start_buffer = timedelta(minutes=margin.time_margin_minutes)
    end_buffer = timedelta(minutes=margin.time_margin_minutes + task_duration_minutes)

    valid_window = (
        db.query(TideWindow)
        .filter(
            TideWindow.berth_id == berth_id,
            TideWindow.min_safe_draft >= required_draft,
            TideWindow.window_start <= planned_time - start_buffer,
            TideWindow.window_end >= planned_time + end_buffer
        )
        .order_by(TideWindow.window_start)
        .first()
    )

    next_window = None
    if not valid_window:
        next_window = (
            db.query(TideWindow)
            .filter(
                TideWindow.berth_id == berth_id,
                TideWindow.min_safe_draft >= required_draft,
                TideWindow.window_start > planned_time
            )
            .order_by(TideWindow.window_start)
            .first()
        )
        if not next_window:
            next_window = (
                db.query(TideWindow)
                .filter(
                    TideWindow.berth_id == berth_id,
                    TideWindow.min_safe_draft >= required_draft
                )
                .order_by(TideWindow.window_start)
                .first()
            )

    return valid_window, next_window, required_draft, margin


def check_pilot_availability(
    db: Session, pilot_id: int, planned_time: datetime,
    duration_minutes: int = 120, exclude_task_id: Optional[int] = None
) -> bool:
    task_end = planned_time + timedelta(minutes=duration_minutes)
    query = db.query(PilotTask).filter(
        PilotTask.pilot_id == pilot_id,
        PilotTask.status.in_([TaskStatus.PENDING, TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS]),
        PilotTask.planned_boarding_time < task_end,
        PilotTask.planned_boarding_time + timedelta(minutes=duration_minutes) > planned_time
    )
    if exclude_task_id:
        query = query.filter(PilotTask.id != exclude_task_id)
    return query.first() is None


def check_vessel_availability(
    db: Session, vessel_id: int, start_time: datetime,
    duration_minutes: int = 120, exclude_task_id: Optional[int] = None
) -> bool:
    vessel = db.query(TransportVessel).filter(TransportVessel.id == vessel_id).first()
    if not vessel or vessel.status == VesselStatus.IN_MAINTENANCE:
        return False
    if vessel.available_from and start_time < vessel.available_from:
        return False
    if vessel.available_to and (start_time + timedelta(minutes=duration_minutes)) > vessel.available_to:
        return False

    task_end = start_time + timedelta(minutes=duration_minutes)
    query = db.query(PilotTask).filter(
        PilotTask.vessel_id == vessel_id,
        PilotTask.status.in_([TaskStatus.PENDING, TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS]),
        PilotTask.planned_boarding_time < task_end,
        PilotTask.planned_boarding_time + timedelta(minutes=duration_minutes) > start_time
    )
    if exclude_task_id:
        query = query.filter(PilotTask.id != exclude_task_id)
    return query.first() is None


def get_pilot_duty_rule(db: Session, pilot_id: int) -> PilotDutyRule:
    personal_rule = (
        db.query(PilotDutyRule)
        .filter(PilotDutyRule.pilot_id == pilot_id, PilotDutyRule.is_active == True)
        .first()
    )
    if personal_rule:
        return personal_rule

    global_rule = (
        db.query(PilotDutyRule)
        .filter(PilotDutyRule.pilot_id.is_(None), PilotDutyRule.is_active == True)
        .first()
    )
    if global_rule:
        return global_rule

    return PilotDutyRule(
        pilot_id=None,
        max_tasks_per_day=5,
        min_rest_minutes_between_tasks=60,
        max_consecutive_work_minutes=480,
        is_active=True
    )


def check_pilot_duty_rule(
    db: Session, pilot_id: int, planned_time: datetime,
    duration_minutes: int = 120, exclude_task_id: Optional[int] = None,
    record_violation: bool = False, task_id: Optional[int] = None
) -> tuple[bool, List[str], List[ViolationType]]:
    """
    检查引航员是否符合执勤约束规则。
    返回 (是否通过, 原因列表, 违规类型列表)
    """
    rule = get_pilot_duty_rule(db, pilot_id)
    reasons = []
    violations = []

    day_start = planned_time.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    query = db.query(PilotTask).filter(
        PilotTask.pilot_id == pilot_id,
        PilotTask.status.in_([TaskStatus.PENDING, TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED]),
        PilotTask.planned_boarding_time >= day_start,
        PilotTask.planned_boarding_time < day_end
    )
    if exclude_task_id:
        query = query.filter(PilotTask.id != exclude_task_id)
    existing_tasks = query.all()
    day_task_count = len(existing_tasks)

    if day_task_count >= rule.max_tasks_per_day:
        reasons.append(
            f"单日任务数已达上限 {rule.max_tasks_per_day} 单（当前 {day_task_count} 单）"
        )
        violations.append(ViolationType.DAILY_TASK_LIMIT)

    min_rest = timedelta(minutes=rule.min_rest_minutes_between_tasks)
    new_start = planned_time
    new_end = planned_time + timedelta(minutes=duration_minutes)

    for t in existing_tasks:
        existing_start = t.planned_boarding_time
        existing_end = t.planned_boarding_time + timedelta(minutes=duration_minutes)

        if existing_end <= new_start:
            gap = (new_start - existing_end).total_seconds() / 60
            if gap < rule.min_rest_minutes_between_tasks:
                reasons.append(
                    f"与前一单（{existing_start.strftime('%H:%M')}结束）间隔仅 {gap:.0f} 分钟，不足最少休息 {rule.min_rest_minutes_between_tasks} 分钟"
                )
                violations.append(ViolationType.REST_INTERVAL)
                break

        if new_end <= existing_start:
            gap = (existing_start - new_end).total_seconds() / 60
            if gap < rule.min_rest_minutes_between_tasks:
                reasons.append(
                    f"与后一单（{existing_start.strftime('%H:%M')}开始）间隔仅 {gap:.0f} 分钟，不足最少休息 {rule.min_rest_minutes_between_tasks} 分钟"
                )
                violations.append(ViolationType.REST_INTERVAL)
                break

    consecutive_limit = rule.max_consecutive_work_minutes
    all_task_slots = []
    for t in existing_tasks:
        all_task_slots.append((t.planned_boarding_time, t.planned_boarding_time + timedelta(minutes=duration_minutes)))
    all_task_slots.append((new_start, new_end))
    all_task_slots.sort(key=lambda x: x[0])

    if all_task_slots:
        cluster_start = all_task_slots[0][0]
        cluster_end = all_task_slots[0][1]
        max_consecutive_minutes = 0

        for i in range(1, len(all_task_slots)):
            t_start, t_end = all_task_slots[i]
            gap = t_start - cluster_end

            if gap < min_rest:
                cluster_end = max(cluster_end, t_end)
            else:
                cluster_minutes = (cluster_end - cluster_start).total_seconds() / 60
                if cluster_minutes > max_consecutive_minutes:
                    max_consecutive_minutes = cluster_minutes
                cluster_start = t_start
                cluster_end = t_end

        cluster_minutes = (cluster_end - cluster_start).total_seconds() / 60
        if cluster_minutes > max_consecutive_minutes:
            max_consecutive_minutes = cluster_minutes

        if max_consecutive_minutes > consecutive_limit:
            reasons.append(
                f"连续工作时长将达 {max_consecutive_minutes:.0f} 分钟，超过上限 {consecutive_limit} 分钟"
            )
            violations.append(ViolationType.CONSECUTIVE_WORK)

    if record_violation and violations:
        for vtype in set(violations):
            log = DutyViolationLog(
                pilot_id=pilot_id,
                task_id=task_id,
                violation_type=vtype,
                violation_detail="; ".join(reasons)
            )
            db.add(log)
        db.flush()

    passed = len(violations) == 0
    return passed, reasons, violations


def save_duty_violation_independent(
    pilot_id: int, task_id: Optional[int],
    violation_types: List[ViolationType], detail: str
):
    from database import SessionLocal
    db = SessionLocal()
    try:
        for vtype in set(violation_types):
            log = DutyViolationLog(
                pilot_id=pilot_id,
                task_id=task_id,
                violation_type=vtype,
                violation_detail=detail
            )
            db.add(log)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


@app.post("/token", response_model=Token, tags=["认证"])
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username, "role": user.role.value},
        expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/users/me", response_model=UserResponse, tags=["用户"])
def read_users_me(current_user: User = Depends(get_current_user)):
    return current_user


@app.post("/users", response_model=UserResponse, tags=["用户"])
def create_user(
    user: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    db_user = db.query(User).filter(User.username == user.username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    hashed_pw = get_password_hash(user.password)
    db_user = User(
        username=user.username,
        hashed_password=hashed_pw,
        full_name=user.full_name,
        role=user.role
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


@app.get("/users", response_model=List[UserResponse], tags=["用户"])
def list_users(
    skip: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    users = db.query(User).offset(skip).limit(limit).all()
    return users


@app.post("/pilots", response_model=PilotResponse, tags=["引航员"])
def create_pilot(
    pilot: PilotCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    db_pilot = db.query(Pilot).filter(Pilot.license_number == pilot.license_number).first()
    if db_pilot:
        raise HTTPException(status_code=400, detail="License number already registered")
    db_pilot = Pilot(**pilot.dict())
    db.add(db_pilot)
    db.commit()
    db.refresh(db_pilot)
    return db_pilot


@app.get("/pilots", response_model=List[PilotResponse], tags=["引航员"])
def list_pilots(
    ship_type: Optional[ShipType] = None,
    qualification: Optional[PilotQualification] = None,
    only_active: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    query = db.query(Pilot)
    if only_active:
        query = query.filter(Pilot.is_active == True)
    if qualification:
        query = query.filter(Pilot.qualification == qualification)
    pilots = query.all()
    if ship_type:
        pilots = [p for p in pilots if ship_type.value in p.certified_ship_types.split(",")]
    return pilots


@app.get("/pilots/{pilot_id}", response_model=PilotResponse, tags=["引航员"])
def get_pilot(
    pilot_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    pilot = db.query(Pilot).filter(Pilot.id == pilot_id).first()
    if not pilot:
        raise HTTPException(status_code=404, detail="Pilot not found")
    return pilot


@app.put("/pilots/{pilot_id}", response_model=PilotResponse, tags=["引航员"])
def update_pilot(
    pilot_id: int, pilot_update: PilotUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    pilot = db.query(Pilot).filter(Pilot.id == pilot_id).first()
    if not pilot:
        raise HTTPException(status_code=404, detail="Pilot not found")
    update_data = pilot_update.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(pilot, key, value)
    db.commit()
    db.refresh(pilot)
    return pilot


@app.post("/berths", response_model=BerthResponse, tags=["泊位"])
def create_berth(
    berth: BerthCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    db_berth = db.query(Berth).filter(Berth.code == berth.code).first()
    if db_berth:
        raise HTTPException(status_code=400, detail="Berth code already exists")
    db_berth = Berth(**berth.dict())
    db.add(db_berth)
    db.commit()
    db.refresh(db_berth)
    return db_berth


@app.get("/berths", response_model=List[BerthResponse], tags=["泊位"])
def list_berths(
    skip: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    return db.query(Berth).offset(skip).limit(limit).all()


@app.post("/ships", response_model=ShipResponse, tags=["船舶"])
def create_ship(
    ship: ShipCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    if ship.imo_number:
        db_ship = db.query(Ship).filter(Ship.imo_number == ship.imo_number).first()
        if db_ship:
            raise HTTPException(status_code=400, detail="IMO number already registered")
    db_ship = Ship(**ship.dict())
    db.add(db_ship)
    db.commit()
    db.refresh(db_ship)
    return db_ship


@app.get("/ships", response_model=List[ShipResponse], tags=["船舶"])
def list_ships(
    ship_type: Optional[ShipType] = None,
    skip: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    query = db.query(Ship)
    if ship_type:
        query = query.filter(Ship.ship_type == ship_type)
    return query.offset(skip).limit(limit).all()


@app.get("/ships/{ship_id}", response_model=ShipResponse, tags=["船舶"])
def get_ship(
    ship_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    ship = db.query(Ship).filter(Ship.id == ship_id).first()
    if not ship:
        raise HTTPException(status_code=404, detail="Ship not found")
    return ship


@app.put("/ships/{ship_id}", response_model=ShipResponse, tags=["船舶"])
def update_ship(
    ship_id: int, ship_update: ShipUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    ship = db.query(Ship).filter(Ship.id == ship_id).first()
    if not ship:
        raise HTTPException(status_code=404, detail="Ship not found")
    update_data = ship_update.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(ship, key, value)
    db.commit()
    db.refresh(ship)
    return ship


@app.post("/tide-windows", response_model=TideWindowResponse, tags=["潮汐窗口"])
def create_tide_window(
    window: TideWindowCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    berth = db.query(Berth).filter(Berth.id == window.berth_id).first()
    if not berth:
        raise HTTPException(status_code=404, detail="Berth not found")
    db_window = TideWindow(**window.dict())
    db.add(db_window)
    db.commit()
    db.refresh(db_window)
    return db_window


@app.get("/tide-windows", response_model=List[TideWindowResponse], tags=["潮汐窗口"])
def list_tide_windows(
    berth_id: Optional[int] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    query = db.query(TideWindow)
    if berth_id:
        query = query.filter(TideWindow.berth_id == berth_id)
    if start_time:
        query = query.filter(TideWindow.window_end >= start_time)
    if end_time:
        query = query.filter(TideWindow.window_start <= end_time)
    return query.order_by(TideWindow.window_start).all()


@app.post("/tide-windows/query", response_model=TaskMatchResult, tags=["潮汐窗口"])
def query_tide_windows(
    query: WindowQuery,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    ship_type = query.ship_type or ShipType.GENERAL
    check_time = query.start_time or datetime.now()

    valid_window, next_window, required_draft, margin = match_tide_window(
        db, query.berth_id, query.ship_draft, ship_type, check_time
    )

    message_parts = []
    if valid_window:
        message_parts.append(f"找到有效潮汐窗口。所需安全吃水: {required_draft:.2f}m")
    else:
        message_parts.append(f"当前无有效潮汐窗口。所需安全吃水: {required_draft:.2f}m")
        if next_window:
            message_parts.append(
                f"下一可用窗口: {next_window.window_start.strftime('%Y-%m-%d %H:%M')} ~ {next_window.window_end.strftime('%H:%M')}"
            )

    available_pilots = (
        db.query(Pilot)
        .filter(Pilot.is_active == True)
        .all()
    )
    if query.ship_type:
        available_pilots = [
            p for p in available_pilots
            if query.ship_type.value in p.certified_ship_types.split(",")
        ]

    duty_excluded = []
    qualified_pilots = []
    for p in available_pilots:
        passed, reasons, _ = check_pilot_duty_rule(db, p.id, check_time)
        if passed:
            qualified_pilots.append(p)
        else:
            duty_excluded.append({
                "pilot_id": p.id,
                "pilot_name": p.name,
                "reasons": reasons
            })

    available_vessels = (
        db.query(TransportVessel)
        .filter(TransportVessel.status == VesselStatus.AVAILABLE)
        .all()
    )

    return TaskMatchResult(
        valid=valid_window is not None,
        message=" ".join(message_parts),
        matched_window=TideWindowSimple.model_validate(valid_window) if valid_window else None,
        next_available_window=TideWindowSimple.model_validate(next_window) if next_window else None,
        available_pilots=[PilotResponse.model_validate(p) for p in qualified_pilots],
        available_vessels=[TransportVesselResponse.model_validate(v) for v in available_vessels],
        duty_excluded_pilots=duty_excluded
    )


@app.post("/safety-margins", response_model=SafetyMarginResponse, tags=["安全余量"])
def create_safety_margin(
    margin: SafetyMarginCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    db_margin = db.query(SafetyMarginConfig).filter(
        SafetyMarginConfig.ship_type == margin.ship_type
    ).first()
    if db_margin:
        db_margin.draft_margin = margin.draft_margin
        db_margin.time_margin_minutes = margin.time_margin_minutes
    else:
        db_margin = SafetyMarginConfig(**margin.dict())
        db.add(db_margin)
    db.commit()
    db.refresh(db_margin)
    return db_margin


@app.get("/safety-margins", response_model=List[SafetyMarginResponse], tags=["安全余量"])
def list_safety_margins(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    return db.query(SafetyMarginConfig).all()


@app.post("/transport-vessels", response_model=TransportVesselResponse, tags=["交通艇"])
def create_transport_vessel(
    vessel: TransportVesselCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    db_vessel = db.query(TransportVessel).filter(TransportVessel.name == vessel.name).first()
    if db_vessel:
        raise HTTPException(status_code=400, detail="Vessel name already exists")
    db_vessel = TransportVessel(**vessel.dict())
    db.add(db_vessel)
    db.commit()
    db.refresh(db_vessel)
    return db_vessel


@app.get("/transport-vessels", response_model=List[TransportVesselResponse], tags=["交通艇"])
def list_transport_vessels(
    status: Optional[VesselStatus] = None,
    skip: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    query = db.query(TransportVessel)
    if status:
        query = query.filter(TransportVessel.status == status)
    return query.offset(skip).limit(limit).all()


@app.get("/transport-vessels/availability", response_model=List[TransportVesselResponse], tags=["交通艇"])
def check_vessels_availability(
    q: VesselAvailabilityQuery = Depends(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    vessels = db.query(TransportVessel).filter(
        TransportVessel.status == VesselStatus.AVAILABLE,
        TransportVessel.capacity >= q.min_capacity
    ).all()

    available = []
    for v in vessels:
        if v.available_from and q.start_time < v.available_from:
            continue
        if v.available_to and q.end_time > v.available_to:
            continue

        duration = int((q.end_time - q.start_time).total_seconds() / 60)
        if check_vessel_availability(db, v.id, q.start_time, duration):
            available.append(v)
    return available


@app.put("/transport-vessels/{vessel_id}", response_model=TransportVesselResponse, tags=["交通艇"])
def update_transport_vessel(
    vessel_id: int, vessel_update: TransportVesselUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    vessel = db.query(TransportVessel).filter(TransportVessel.id == vessel_id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    update_data = vessel_update.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(vessel, key, value)
    db.commit()
    db.refresh(vessel)
    return vessel


@app.get("/transport-vessels/{vessel_id}", response_model=TransportVesselResponse, tags=["交通艇"])
def get_transport_vessel(
    vessel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    vessel = db.query(TransportVessel).filter(TransportVessel.id == vessel_id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    return vessel


@app.get("/duty-rules", response_model=List[PilotDutyRuleResponse], tags=["执勤规则"])
def list_duty_rules(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    query = db.query(PilotDutyRule)
    if not include_inactive:
        query = query.filter(PilotDutyRule.is_active == True)
    rules = query.order_by(PilotDutyRule.pilot_id.is_(None).desc(), PilotDutyRule.id).all()

    result = []
    for r in rules:
        resp = PilotDutyRuleResponse(
            id=r.id,
            pilot_id=r.pilot_id,
            max_tasks_per_day=r.max_tasks_per_day,
            min_rest_minutes_between_tasks=r.min_rest_minutes_between_tasks,
            max_consecutive_work_minutes=r.max_consecutive_work_minutes,
            is_global=r.pilot_id is None,
            is_active=r.is_active,
            created_at=r.created_at,
            updated_at=r.updated_at,
            pilot=PilotResponse.model_validate(r.pilot) if r.pilot else None
        )
        result.append(resp)
    return result


@app.get("/duty-rules/global", response_model=PilotDutyRuleResponse, tags=["执勤规则"])
def get_global_duty_rule(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    rule = db.query(PilotDutyRule).filter(
        PilotDutyRule.pilot_id.is_(None),
        PilotDutyRule.is_active == True
    ).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Global duty rule not found")
    return PilotDutyRuleResponse(
        id=rule.id,
        pilot_id=rule.pilot_id,
        max_tasks_per_day=rule.max_tasks_per_day,
        min_rest_minutes_between_tasks=rule.min_rest_minutes_between_tasks,
        max_consecutive_work_minutes=rule.max_consecutive_work_minutes,
        is_global=True,
        is_active=rule.is_active,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
        pilot=None
    )


@app.get("/duty-rules/pilot/{pilot_id}", response_model=PilotDutyRuleResponse, tags=["执勤规则"])
def get_pilot_effective_duty_rule(
    pilot_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    pilot = db.query(Pilot).filter(Pilot.id == pilot_id).first()
    if not pilot:
        raise HTTPException(status_code=404, detail="Pilot not found")
    rule = get_pilot_duty_rule(db, pilot_id)
    return PilotDutyRuleResponse(
        id=rule.id if rule.id else 0,
        pilot_id=rule.pilot_id,
        max_tasks_per_day=rule.max_tasks_per_day,
        min_rest_minutes_between_tasks=rule.min_rest_minutes_between_tasks,
        max_consecutive_work_minutes=rule.max_consecutive_work_minutes,
        is_global=rule.pilot_id is None,
        is_active=rule.is_active,
        created_at=rule.created_at if rule.created_at else datetime.now(),
        updated_at=rule.updated_at if rule.updated_at else datetime.now(),
        pilot=PilotResponse.model_validate(pilot) if pilot else None
    )


@app.post("/duty-rules", response_model=PilotDutyRuleResponse, tags=["执勤规则"])
def create_duty_rule(
    rule_data: PilotDutyRuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    if rule_data.pilot_id:
        pilot = db.query(Pilot).filter(Pilot.id == rule_data.pilot_id).first()
        if not pilot:
            raise HTTPException(status_code=404, detail="Pilot not found")
        existing = db.query(PilotDutyRule).filter(
            PilotDutyRule.pilot_id == rule_data.pilot_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="该引航员已有执勤规则，请使用更新接口")
    else:
        existing = db.query(PilotDutyRule).filter(PilotDutyRule.pilot_id.is_(None)).first()
        if existing:
            raise HTTPException(status_code=400, detail="全局规则已存在，请使用更新接口")

    db_rule = PilotDutyRule(**rule_data.dict())
    db.add(db_rule)
    db.flush()

    remark = "创建全局执勤规则" if rule_data.pilot_id is None else f"创建引航员 {rule_data.pilot_id} 的执勤规则"
    db.add(DutyRuleAuditLog(
        rule_id=db_rule.id,
        pilot_id=rule_data.pilot_id,
        operator_id=current_user.id,
        action=AuditAction.DUTY_RULE_CREATED,
        is_global=rule_data.pilot_id is None,
        new_value=f"max_tasks={rule_data.max_tasks_per_day}, min_rest={rule_data.min_rest_minutes_between_tasks}min, max_consecutive={rule_data.max_consecutive_work_minutes}min",
        remark=remark
    ))
    db.commit()
    db.refresh(db_rule)

    return PilotDutyRuleResponse(
        id=db_rule.id,
        pilot_id=db_rule.pilot_id,
        max_tasks_per_day=db_rule.max_tasks_per_day,
        min_rest_minutes_between_tasks=db_rule.min_rest_minutes_between_tasks,
        max_consecutive_work_minutes=db_rule.max_consecutive_work_minutes,
        is_global=db_rule.pilot_id is None,
        is_active=db_rule.is_active,
        created_at=db_rule.created_at,
        updated_at=db_rule.updated_at,
        pilot=PilotResponse.model_validate(db_rule.pilot) if db_rule.pilot else None
    )


@app.put("/duty-rules/{rule_id}", response_model=PilotDutyRuleResponse, tags=["执勤规则"])
def update_duty_rule(
    rule_id: int,
    rule_update: PilotDutyRuleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    rule = db.query(PilotDutyRule).filter(PilotDutyRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Duty rule not found")

    old_value = (
        f"max_tasks={rule.max_tasks_per_day}, "
        f"min_rest={rule.min_rest_minutes_between_tasks}min, "
        f"max_consecutive={rule.max_consecutive_work_minutes}min, "
        f"active={rule.is_active}"
    )

    update_data = rule_update.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(rule, key, value)

    new_value = (
        f"max_tasks={rule.max_tasks_per_day}, "
        f"min_rest={rule.min_rest_minutes_between_tasks}min, "
        f"max_consecutive={rule.max_consecutive_work_minutes}min, "
        f"active={rule.is_active}"
    )

    remark = "更新全局执勤规则" if rule.pilot_id is None else f"更新引航员 {rule.pilot_id} 的执勤规则"
    db.add(DutyRuleAuditLog(
        rule_id=rule.id,
        pilot_id=rule.pilot_id,
        operator_id=current_user.id,
        action=AuditAction.DUTY_RULE_UPDATED,
        is_global=rule.pilot_id is None,
        old_value=old_value,
        new_value=new_value,
        remark=remark
    ))
    db.commit()
    db.refresh(rule)

    return PilotDutyRuleResponse(
        id=rule.id,
        pilot_id=rule.pilot_id,
        max_tasks_per_day=rule.max_tasks_per_day,
        min_rest_minutes_between_tasks=rule.min_rest_minutes_between_tasks,
        max_consecutive_work_minutes=rule.max_consecutive_work_minutes,
        is_global=rule.pilot_id is None,
        is_active=rule.is_active,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
        pilot=PilotResponse.model_validate(rule.pilot) if rule.pilot else None
    )


@app.delete("/duty-rules/{rule_id}", tags=["执勤规则"])
def delete_duty_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    rule = db.query(PilotDutyRule).filter(PilotDutyRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Duty rule not found")

    remark = "删除全局执勤规则" if rule.pilot_id is None else f"删除引航员 {rule.pilot_id} 的执勤规则"
    db.add(DutyRuleAuditLog(
        rule_id=rule.id,
        pilot_id=rule.pilot_id,
        operator_id=current_user.id,
        action=AuditAction.DUTY_RULE_DELETED,
        is_global=rule.pilot_id is None,
        old_value=f"max_tasks={rule.max_tasks_per_day}, min_rest={rule.min_rest_minutes_between_tasks}min",
        remark=remark
    ))

    db.delete(rule)
    db.commit()
    return {"message": "执勤规则已删除"}


@app.post("/tasks", response_model=TaskMatchResult, tags=["任务"])
def create_pilot_task(
    task: PilotTaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    ship = db.query(Ship).filter(Ship.id == task.ship_id).first()
    if not ship:
        raise HTTPException(status_code=404, detail="Ship not found")
    berth = db.query(Berth).filter(Berth.id == task.berth_id).first()
    if not berth:
        raise HTTPException(status_code=404, detail="Berth not found")

    if ship.draft > berth.max_draft:
        return TaskMatchResult(
            valid=False,
            message=f"船舶吃水({ship.draft}m)超过泊位最大允许吃水({berth.max_draft}m)",
            available_pilots=[],
            available_vessels=[]
        )
    if ship.length > berth.max_length:
        return TaskMatchResult(
            valid=False,
            message=f"船舶长度({ship.length}m)超过泊位最大允许长度({berth.max_length}m)",
            available_pilots=[],
            available_vessels=[]
        )

    valid_window, next_window, required_draft, margin = match_tide_window(
        db, task.berth_id, ship.draft, ship.ship_type, task.planned_boarding_time
    )

    all_pilots = (
        db.query(Pilot)
        .filter(Pilot.is_active == True)
        .all()
    )
    qualified_pilots = [
        p for p in all_pilots
        if ship.ship_type.value in p.certified_ship_types.split(",")
    ]
    qualified_pilot_ids = {p.id for p in qualified_pilots}

    time_available_pilots = [
        p for p in qualified_pilots
        if check_pilot_availability(db, p.id, task.planned_boarding_time)
    ]

    duty_excluded = []
    duty_ok_pilots = []
    for p in time_available_pilots:
        passed, reasons, _ = check_pilot_duty_rule(db, p.id, task.planned_boarding_time)
        if passed:
            duty_ok_pilots.append(p)
        else:
            duty_excluded.append({
                "pilot_id": p.id,
                "pilot_name": p.name,
                "reasons": reasons,
                "exclude_reason": "duty_rule"
            })

    time_conflict_pilots = [
        p for p in qualified_pilots
        if p.id not in {tp.id for tp in time_available_pilots}
    ]
    for p in time_conflict_pilots:
        duty_excluded.append({
            "pilot_id": p.id,
            "pilot_name": p.name,
            "reasons": ["该时段已有任务时间冲突"],
            "exclude_reason": "time_conflict"
        })

    unqualified_pilots = [
        p for p in all_pilots
        if p.id not in qualified_pilot_ids
    ]
    for p in unqualified_pilots:
        duty_excluded.append({
            "pilot_id": p.id,
            "pilot_name": p.name,
            "reasons": [f"资质不匹配船舶类型 {ship.ship_type.value}"],
            "exclude_reason": "qualification"
        })

    available_vessels = (
        db.query(TransportVessel)
        .filter(TransportVessel.status == VesselStatus.AVAILABLE)
        .all()
    )
    available_vessels = [
        v for v in available_vessels
        if check_vessel_availability(db, v.id, task.planned_boarding_time)
    ]

    message_parts = []
    if valid_window:
        message_parts.append(
            f"潮汐窗口校验通过。安全吃水要求: {required_draft:.2f}m, 窗口最小吃水: {valid_window.min_safe_draft:.2f}m"
        )
    else:
        message_parts.append(
            f"超出潮汐安全窗口！安全吃水要求: {required_draft:.2f}m"
        )
        if next_window:
            message_parts.append(
                f"建议下一可用窗口: {next_window.window_start.strftime('%Y-%m-%d %H:%M')} ~ {next_window.window_end.strftime('%H:%M')} (最小吃水: {next_window.min_safe_draft:.2f}m)"
            )

    if not duty_ok_pilots:
        duty_count = len([d for d in duty_excluded if d.get("exclude_reason") == "duty_rule"])
        time_count = len([d for d in duty_excluded if d.get("exclude_reason") == "time_conflict"])
        qual_count = len([d for d in duty_excluded if d.get("exclude_reason") == "qualification"])
        msg_parts = []
        if qual_count > 0:
            msg_parts.append(f"{qual_count}人资质不符")
        if time_count > 0:
            msg_parts.append(f"{time_count}人时间冲突")
        if duty_count > 0:
            msg_parts.append(f"{duty_count}人被执勤规则限制")
        message_parts.append(f"；无可用引航员（{'、'.join(msg_parts)}）")

    if not available_vessels:
        message_parts.append("；无可用交通艇")

    if valid_window and duty_ok_pilots and available_vessels:
        task_number = generate_task_number(db)
        db_task = PilotTask(
            task_number=task_number,
            ship_id=task.ship_id,
            berth_id=task.berth_id,
            tide_window_id=valid_window.id,
            task_type=task.task_type,
            planned_boarding_time=task.planned_boarding_time,
            boarding_point=task.boarding_point,
            notes=task.notes,
            status=TaskStatus.PENDING
        )
        db.add(db_task)
        db.flush()
        create_audit_log(db, db_task.id, current_user.id, AuditAction.TASK_CREATED, remark=f"创建任务 {task_number}")
        db.commit()
        db.refresh(db_task)

        return TaskMatchResult(
            task_id=db_task.id,
            valid=True,
            message="任务创建成功。" + " ".join(message_parts),
            matched_window=TideWindowSimple.model_validate(valid_window),
            next_available_window=None,
            available_pilots=[PilotResponse.model_validate(p) for p in duty_ok_pilots],
            available_vessels=[TransportVesselResponse.model_validate(v) for v in available_vessels],
            duty_excluded_pilots=duty_excluded
        )
    else:
        return TaskMatchResult(
            valid=False,
            message=" ".join(message_parts),
            matched_window=TideWindowSimple.model_validate(valid_window) if valid_window else None,
            next_available_window=TideWindowSimple.model_validate(next_window) if next_window else None,
            available_pilots=[PilotResponse.model_validate(p) for p in duty_ok_pilots],
            available_vessels=[TransportVesselResponse.model_validate(v) for v in available_vessels],
            duty_excluded_pilots=duty_excluded
        )


@app.get("/tasks", response_model=List[PilotTaskResponse], tags=["任务"])
def list_tasks(
    status: Optional[TaskStatus] = None,
    pilot_id: Optional[int] = None,
    skip: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    query = db.query(PilotTask)
    if status:
        query = query.filter(PilotTask.status == status)
    if pilot_id:
        query = query.filter(PilotTask.pilot_id == pilot_id)
    return query.order_by(PilotTask.created_at.desc()).offset(skip).limit(limit).all()


@app.get("/tasks/{task_id}", response_model=PilotTaskResponse, tags=["任务"])
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    task = db.query(PilotTask).filter(PilotTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/tasks/{task_id}/assign", response_model=PilotTaskResponse, tags=["任务"])
def assign_task(
    task_id: int, assign: PilotTaskAssign,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    task = db.query(PilotTask).filter(PilotTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in [TaskStatus.PENDING, TaskStatus.ASSIGNED]:
        raise HTTPException(status_code=400, detail=f"Cannot assign task in status: {task.status.value}")

    if not task.tide_window_id:
        raise HTTPException(status_code=400, detail="任务未匹配有效潮汐窗口，无法派单")

    window = db.query(TideWindow).filter(TideWindow.id == task.tide_window_id).first()
    ship = db.query(Ship).filter(Ship.id == task.ship_id).first()
    margin = get_safety_margin(db, ship.ship_type)
    start_margin = timedelta(minutes=margin.time_margin_minutes)
    planned = task.planned_boarding_time

    if planned - start_margin < window.window_start or planned + timedelta(hours=2) + start_margin > window.window_end:
        raise HTTPException(
            status_code=400,
            detail=f"计划登轮时间超出潮汐安全窗口。窗口范围: {window.window_start} ~ {window.window_end}"
        )

    pilot = db.query(Pilot).filter(Pilot.id == assign.pilot_id).first()
    if not pilot:
        raise HTTPException(status_code=404, detail="Pilot not found")
    if not pilot.is_active:
        raise HTTPException(status_code=400, detail="引航员已停用")
    if ship.ship_type.value not in pilot.certified_ship_types.split(","):
        raise HTTPException(status_code=400, detail=f"引航员资质不匹配船舶类型 {ship.ship_type.value}")

    if not check_pilot_availability(db, assign.pilot_id, planned, exclude_task_id=task_id):
        raise HTTPException(status_code=400, detail="引航员该时段已有任务冲突")

    duty_passed, duty_reasons, duty_violations = check_pilot_duty_rule(
        db, assign.pilot_id, planned, exclude_task_id=task_id,
        record_violation=False, task_id=task_id
    )
    if not duty_passed:
        save_duty_violation_independent(
            assign.pilot_id, task_id, duty_violations, "; ".join(duty_reasons)
        )
        raise HTTPException(
            status_code=400,
            detail="引航员不符合执勤约束规则：" + "；".join(duty_reasons)
        )

    vessel = db.query(TransportVessel).filter(TransportVessel.id == assign.vessel_id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    if vessel.status == VesselStatus.IN_MAINTENANCE:
        raise HTTPException(status_code=400, detail="交通艇维护中，不可用")
    if not check_vessel_availability(db, assign.vessel_id, planned, exclude_task_id=task_id):
        raise HTTPException(status_code=400, detail="交通艇该时段已被占用")

    old_pilot = task.pilot_id
    old_vessel = task.vessel_id
    is_reassign = task.status == TaskStatus.ASSIGNED

    task.pilot_id = assign.pilot_id
    task.vessel_id = assign.vessel_id
    task.status = TaskStatus.ASSIGNED

    if is_reassign:
        create_audit_log(
            db, task.id, current_user.id, AuditAction.TASK_REASSIGNED,
            old_value=f"pilot={old_pilot}, vessel={old_vessel}",
            new_value=f"pilot={assign.pilot_id}, vessel={assign.vessel_id}",
            remark="任务改派"
        )
    else:
        create_audit_log(
            db, task.id, current_user.id, AuditAction.TASK_ASSIGNED,
            old_value=None,
            new_value=f"pilot={assign.pilot_id}, vessel={assign.vessel_id}",
            remark="任务派单"
        )
    db.commit()
    db.refresh(task)
    return task


@app.post("/tasks/{task_id}/start", response_model=PilotTaskResponse, tags=["任务"])
def start_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    task = db.query(PilotTask).filter(PilotTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.ASSIGNED:
        raise HTTPException(status_code=400, detail=f"Cannot start task in status: {task.status.value}")

    task.status = TaskStatus.IN_PROGRESS
    task.actual_boarding_time = datetime.now()

    create_audit_log(db, task.id, current_user.id, AuditAction.TASK_STARTED, remark="任务开始执行")
    db.commit()
    db.refresh(task)
    return task


@app.post("/tasks/{task_id}/complete", response_model=PilotTaskResponse, tags=["任务"])
def complete_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    task = db.query(PilotTask).filter(PilotTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.IN_PROGRESS:
        raise HTTPException(status_code=400, detail=f"Cannot complete task in status: {task.status.value}")

    task.status = TaskStatus.COMPLETED
    task.actual_completion_time = datetime.now()

    create_audit_log(db, task.id, current_user.id, AuditAction.TASK_COMPLETED, remark="任务完成")
    db.commit()
    db.refresh(task)
    return task


@app.post("/tasks/{task_id}/delay", response_model=PilotTaskResponse, tags=["任务"])
def record_delay(
    task_id: int, delay: PilotTaskDelay,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    task = db.query(PilotTask).filter(PilotTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status in [TaskStatus.COMPLETED, TaskStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail="任务已结束，无法记录延误")

    task.delay_minutes = delay.delay_minutes
    task.delay_reason = delay.delay_reason

    create_audit_log(
        db, task.id, current_user.id, AuditAction.DELAY_RECORDED,
        old_value=str(task.delay_minutes),
        new_value=str(delay.delay_minutes),
        remark=delay.delay_reason
    )
    db.commit()
    db.refresh(task)
    return task


@app.post("/tasks/{task_id}/cancel", response_model=PilotTaskResponse, tags=["任务"])
def cancel_task(
    task_id: int, cancel: PilotTaskCancel,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    task = db.query(PilotTask).filter(PilotTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status in [TaskStatus.COMPLETED, TaskStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail="任务已结束，无法取消")

    old_status = task.status.value
    task.status = TaskStatus.CANCELLED

    create_audit_log(
        db, task.id, current_user.id, AuditAction.TASK_CANCELLED,
        old_value=old_status,
        new_value=TaskStatus.CANCELLED.value,
        remark=cancel.reason
    )
    db.commit()
    db.refresh(task)
    return task


@app.get("/tasks/{task_id}/audit-logs", response_model=List[AuditLogResponse], tags=["审计"])
def get_task_audit_logs(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    task = db.query(PilotTask).filter(PilotTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.task_id == task_id)
        .order_by(AuditLog.created_at.desc())
        .all()
    )
    return logs


@app.get("/stats/window-utilization", response_model=List[WindowUtilizationStats], tags=["统计"])
def get_window_utilization(
    period_start: datetime,
    period_end: datetime,
    berth_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    berths = db.query(Berth).all()
    if berth_id:
        berths = [b for b in berths if b.id == berth_id]

    results = []
    for berth in berths:
        windows = (
            db.query(TideWindow)
            .filter(
                TideWindow.berth_id == berth.id,
                TideWindow.window_start >= period_start,
                TideWindow.window_end <= period_end
            )
            .all()
        )
        total = len(windows)
        window_ids = [w.id for w in windows]

        used = (
            db.query(func.count(PilotTask.id))
            .filter(
                PilotTask.tide_window_id.in_(window_ids),
                PilotTask.status.in_([TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED])
            )
            .scalar()
        ) or 0

        rate = (used / total * 100) if total > 0 else 0.0
        results.append(WindowUtilizationStats(
            berth_id=berth.id,
            berth_name=berth.name,
            total_windows=total,
            used_windows=used,
            utilization_rate=round(rate, 2),
            period_start=period_start,
            period_end=period_end
        ))
    return results


@app.get("/stats/delays", response_model=DelayStats, tags=["统计"])
def get_delay_stats(
    period_start: datetime,
    period_end: datetime,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    tasks = (
        db.query(PilotTask)
        .filter(
            PilotTask.created_at >= period_start,
            PilotTask.created_at <= period_end
        )
        .all()
    )
    total = len(tasks)
    delayed = [t for t in tasks if t.delay_minutes > 0]
    delayed_count = len(delayed)

    delay_rate = (delayed_count / total * 100) if total > 0 else 0.0
    avg_delay = (sum(t.delay_minutes for t in delayed) / delayed_count) if delayed_count > 0 else 0.0

    reason_counter = Counter(t.delay_reason for t in delayed if t.delay_reason)
    delay_reasons = [{"reason": r, "count": c} for r, c in reason_counter.most_common()]

    return DelayStats(
        total_tasks=total,
        delayed_tasks=delayed_count,
        delay_rate=round(delay_rate, 2),
        average_delay_minutes=round(avg_delay, 2),
        delay_reasons=delay_reasons,
        period_start=period_start,
        period_end=period_end
    )


@app.get("/stats/pilot-duty", response_model=PilotDutyStatsResponse, tags=["统计"])
def get_pilot_duty_stats(
    period_start: datetime,
    period_end: datetime,
    pilot_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    pilots = db.query(Pilot).filter(Pilot.is_active == True).all()
    if pilot_id:
        pilots = [p for p in pilots if p.id == pilot_id]

    pilot_stats = []
    for pilot in pilots:
        tasks = (
            db.query(PilotTask)
            .filter(
                PilotTask.pilot_id == pilot.id,
                PilotTask.planned_boarding_time >= period_start,
                PilotTask.planned_boarding_time <= period_end
            )
            .all()
        )
        total = len(tasks)
        completed = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)
        in_progress = sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS)

        total_duty_minutes = 0.0
        for t in tasks:
            if t.actual_boarding_time and t.actual_completion_time:
                total_duty_minutes += (t.actual_completion_time - t.actual_boarding_time).total_seconds() / 60
            else:
                total_duty_minutes += 120

        violations = (
            db.query(DutyViolationLog)
            .filter(
                DutyViolationLog.pilot_id == pilot.id,
                DutyViolationLog.rejected_at >= period_start,
                DutyViolationLog.rejected_at <= period_end
            )
            .all()
        )
        total_rejections = len(violations)
        daily_rej = sum(1 for v in violations if v.violation_type == ViolationType.DAILY_TASK_LIMIT)
        rest_rej = sum(1 for v in violations if v.violation_type == ViolationType.REST_INTERVAL)
        cont_rej = sum(1 for v in violations if v.violation_type == ViolationType.CONSECUTIVE_WORK)

        pilot_stats.append(PilotDutyStatsItem(
            pilot_id=pilot.id,
            pilot_name=pilot.name,
            license_number=pilot.license_number,
            total_tasks=total,
            completed_tasks=completed,
            in_progress_tasks=in_progress,
            total_duty_minutes=round(total_duty_minutes, 2),
            duty_rejections=total_rejections,
            daily_limit_rejections=daily_rej,
            rest_interval_rejections=rest_rej,
            continuous_limit_rejections=cont_rej,
            period_start=period_start,
            period_end=period_end
        ))

    return PilotDutyStatsResponse(
        period_start=period_start,
        period_end=period_end,
        pilot_stats=pilot_stats
    )


@app.get("/duty-violations", response_model=List[DutyViolationResponse], tags=["执勤规则"])
def list_duty_violations(
    pilot_id: Optional[int] = None,
    period_start: Optional[datetime] = None,
    period_end: Optional[datetime] = None,
    skip: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    query = db.query(DutyViolationLog)
    if pilot_id:
        query = query.filter(DutyViolationLog.pilot_id == pilot_id)
    if period_start:
        query = query.filter(DutyViolationLog.rejected_at >= period_start)
    if period_end:
        query = query.filter(DutyViolationLog.rejected_at <= period_end)

    logs = query.order_by(DutyViolationLog.rejected_at.desc()).offset(skip).limit(limit).all()
    return logs


@app.get("/duty-rules/audit-logs", response_model=List[DutyRuleAuditLogResponse], tags=["执勤规则"])
def list_duty_rule_audit_logs(
    rule_id: Optional[int] = None,
    pilot_id: Optional[int] = None,
    is_global: Optional[bool] = None,
    action: Optional[AuditAction] = None,
    period_start: Optional[datetime] = None,
    period_end: Optional[datetime] = None,
    skip: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_dispatcher)
):
    query = db.query(DutyRuleAuditLog)
    if rule_id:
        query = query.filter(DutyRuleAuditLog.rule_id == rule_id)
    if pilot_id:
        query = query.filter(DutyRuleAuditLog.pilot_id == pilot_id)
    if is_global is not None:
        query = query.filter(DutyRuleAuditLog.is_global == is_global)
    if action:
        query = query.filter(DutyRuleAuditLog.action == action)
    if period_start:
        query = query.filter(DutyRuleAuditLog.created_at >= period_start)
    if period_end:
        query = query.filter(DutyRuleAuditLog.created_at <= period_end)

    logs = query.order_by(DutyRuleAuditLog.created_at.desc()).offset(skip).limit(limit).all()
    return logs


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
