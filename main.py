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
    ShipType, PilotQualification, VesselStatus, AuditAction
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
    WindowUtilizationStats, DelayStats
)
from security import (
    get_password_hash, authenticate_user, create_access_token,
    get_current_user, get_current_active_dispatcher, get_current_admin,
    ACCESS_TOKEN_EXPIRE_MINUTES
)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="引航任务调度系统", version="1.0.0")


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
    duration_minutes: int = 120
) -> bool:
    task_end = planned_time + timedelta(minutes=duration_minutes)
    conflicting = (
        db.query(PilotTask)
        .filter(
            PilotTask.pilot_id == pilot_id,
            PilotTask.status.in_([TaskStatus.PENDING, TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS]),
            PilotTask.planned_boarding_time < task_end,
            PilotTask.planned_boarding_time + timedelta(minutes=duration_minutes) > planned_time
        )
        .first()
    )
    return conflicting is None


def check_vessel_availability(
    db: Session, vessel_id: int, start_time: datetime,
    duration_minutes: int = 120
) -> bool:
    vessel = db.query(TransportVessel).filter(TransportVessel.id == vessel_id).first()
    if not vessel or vessel.status == VesselStatus.IN_MAINTENANCE:
        return False
    if vessel.available_from and start_time < vessel.available_from:
        return False
    if vessel.available_to and (start_time + timedelta(minutes=duration_minutes)) > vessel.available_to:
        return False

    task_end = start_time + timedelta(minutes=duration_minutes)
    conflicting = (
        db.query(PilotTask)
        .filter(
            PilotTask.vessel_id == vessel_id,
            PilotTask.status.in_([TaskStatus.PENDING, TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS]),
            PilotTask.planned_boarding_time < task_end,
            PilotTask.planned_boarding_time + timedelta(minutes=duration_minutes) > start_time
        )
        .first()
    )
    return conflicting is None


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
        available_pilots=[PilotResponse.model_validate(p) for p in available_pilots],
        available_vessels=[TransportVesselResponse.model_validate(v) for v in available_vessels]
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

    available_pilots = (
        db.query(Pilot)
        .filter(Pilot.is_active == True)
        .all()
    )
    available_pilots = [
        p for p in available_pilots
        if ship.ship_type.value in p.certified_ship_types.split(",")
    ]
    available_pilots = [
        p for p in available_pilots
        if check_pilot_availability(db, p.id, task.planned_boarding_time)
    ]

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

    if not available_pilots:
        message_parts.append("；当前无匹配资质且可用的引航员")
    if not available_vessels:
        message_parts.append("；当前无可用的交通艇")

    if valid_window and available_pilots and available_vessels:
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
            available_pilots=[PilotResponse.model_validate(p) for p in available_pilots],
            available_vessels=[TransportVesselResponse.model_validate(v) for v in available_vessels]
        )
    else:
        return TaskMatchResult(
            valid=False,
            message=" ".join(message_parts),
            matched_window=TideWindowSimple.model_validate(valid_window) if valid_window else None,
            next_available_window=TideWindowSimple.model_validate(next_window) if next_window else None,
            available_pilots=[PilotResponse.model_validate(p) for p in available_pilots],
            available_vessels=[TransportVesselResponse.model_validate(v) for v in available_vessels]
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

    if not check_pilot_availability(db, assign.pilot_id, planned):
        raise HTTPException(status_code=400, detail="引航员该时段已有任务冲突")

    vessel = db.query(TransportVessel).filter(TransportVessel.id == assign.vessel_id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    if vessel.status == VesselStatus.IN_MAINTENANCE:
        raise HTTPException(status_code=400, detail="交通艇维护中，不可用")
    if not check_vessel_availability(db, assign.vessel_id, planned):
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
