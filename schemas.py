from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List
from enum import Enum


class UserRole(str, Enum):
    DISPATCHER = "dispatcher"
    ADMIN = "admin"
    PILOT = "pilot"


class TaskStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ShipType(str, Enum):
    CONTAINER = "container"
    BULK = "bulk"
    TANKER = "tanker"
    PASSENGER = "passenger"
    GENERAL = "general"
    RORO = "roro"


class PilotQualification(str, Enum):
    JUNIOR = "junior"
    SENIOR = "senior"
    MASTER = "master"


class VesselStatus(str, Enum):
    AVAILABLE = "available"
    IN_MAINTENANCE = "maintenance"
    IN_USE = "in_use"


class AuditAction(str, Enum):
    TASK_CREATED = "task_created"
    TASK_ASSIGNED = "task_assigned"
    TASK_REASSIGNED = "task_reassigned"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_CANCELLED = "task_cancelled"
    DELAY_RECORDED = "delay_recorded"


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: Optional[str] = None
    role: Optional[UserRole] = None


class UserBase(BaseModel):
    username: str = Field(..., max_length=50)
    full_name: str = Field(..., max_length=100)
    role: UserRole = UserRole.DISPATCHER


class UserCreate(UserBase):
    password: str = Field(..., min_length=6)


class UserResponse(UserBase):
    id: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class PilotBase(BaseModel):
    license_number: str = Field(..., max_length=50)
    name: str = Field(..., max_length=100)
    phone: Optional[str] = Field(None, max_length=20)
    qualification: PilotQualification
    certified_ship_types: str = Field(..., max_length=255)


class PilotCreate(PilotBase):
    user_id: Optional[int] = None


class PilotUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    qualification: Optional[PilotQualification] = None
    certified_ship_types: Optional[str] = None
    is_active: Optional[bool] = None


class PilotResponse(PilotBase):
    id: int
    is_active: bool
    created_at: datetime
    user_id: Optional[int] = None

    class Config:
        from_attributes = True


class BerthBase(BaseModel):
    code: str = Field(..., max_length=20)
    name: str = Field(..., max_length=100)
    max_draft: float
    max_length: float


class BerthCreate(BerthBase):
    pass


class BerthResponse(BerthBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class ShipBase(BaseModel):
    imo_number: Optional[str] = Field(None, max_length=20)
    name: str = Field(..., max_length=100)
    ship_type: ShipType
    draft: float
    length: float
    flag: Optional[str] = Field(None, max_length=50)


class ShipCreate(ShipBase):
    pass


class ShipUpdate(BaseModel):
    name: Optional[str] = None
    ship_type: Optional[ShipType] = None
    draft: Optional[float] = None
    length: Optional[float] = None
    flag: Optional[str] = None


class ShipResponse(ShipBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class TideWindowBase(BaseModel):
    berth_id: int
    window_start: datetime
    window_end: datetime
    min_safe_draft: float
    tide_height: float


class TideWindowCreate(TideWindowBase):
    pass


class TideWindowResponse(TideWindowBase):
    id: int
    berth: BerthResponse
    created_at: datetime

    class Config:
        from_attributes = True


class TideWindowSimple(TideWindowBase):
    id: int

    class Config:
        from_attributes = True


class WindowQuery(BaseModel):
    berth_id: int
    ship_draft: float
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    ship_type: Optional[ShipType] = None


class SafetyMarginBase(BaseModel):
    ship_type: ShipType
    draft_margin: float = 0.3
    time_margin_minutes: int = 30


class SafetyMarginCreate(SafetyMarginBase):
    pass


class SafetyMarginResponse(SafetyMarginBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class TransportVesselBase(BaseModel):
    name: str = Field(..., max_length=100)
    capacity: int
    status: VesselStatus = VesselStatus.AVAILABLE
    available_from: Optional[datetime] = None
    available_to: Optional[datetime] = None
    maintenance_notes: Optional[str] = None


class TransportVesselCreate(TransportVesselBase):
    pass


class TransportVesselUpdate(BaseModel):
    name: Optional[str] = None
    capacity: Optional[int] = None
    status: Optional[VesselStatus] = None
    available_from: Optional[datetime] = None
    available_to: Optional[datetime] = None
    maintenance_notes: Optional[str] = None


class TransportVesselResponse(TransportVesselBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class VesselAvailabilityQuery(BaseModel):
    start_time: datetime
    end_time: datetime
    min_capacity: Optional[int] = 1


class PilotTaskBase(BaseModel):
    ship_id: int
    berth_id: int
    task_type: str = Field(..., max_length=20)
    planned_boarding_time: datetime
    boarding_point: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = None


class PilotTaskCreate(PilotTaskBase):
    pass


class PilotTaskAssign(BaseModel):
    pilot_id: int
    vessel_id: int


class PilotTaskStatusUpdate(BaseModel):
    status: TaskStatus


class PilotTaskDelay(BaseModel):
    delay_minutes: int
    delay_reason: str


class PilotTaskCancel(BaseModel):
    reason: str


class PilotTaskResponse(BaseModel):
    id: int
    task_number: str
    ship_id: int
    berth_id: int
    pilot_id: Optional[int] = None
    vessel_id: Optional[int] = None
    tide_window_id: Optional[int] = None
    task_type: str
    planned_boarding_time: datetime
    status: TaskStatus
    boarding_point: Optional[str] = None
    notes: Optional[str] = None
    actual_boarding_time: Optional[datetime] = None
    actual_completion_time: Optional[datetime] = None
    delay_minutes: int = 0
    delay_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    ship: ShipResponse
    berth: BerthResponse
    pilot: Optional[PilotResponse] = None
    vessel: Optional[TransportVesselResponse] = None
    tide_window: Optional[TideWindowSimple] = None

    class Config:
        from_attributes = True


class TaskMatchResult(BaseModel):
    task_id: Optional[int] = None
    valid: bool
    message: str
    matched_window: Optional[TideWindowSimple] = None
    next_available_window: Optional[TideWindowSimple] = None
    available_pilots: List[PilotResponse] = []
    available_vessels: List[TransportVesselResponse] = []


class AuditLogBase(BaseModel):
    task_id: int
    action: AuditAction
    remark: Optional[str] = None


class AuditLogResponse(BaseModel):
    id: int
    task_id: int
    operator_id: int
    action: AuditAction
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    remark: Optional[str] = None
    created_at: datetime

    operator: UserResponse

    class Config:
        from_attributes = True


class WindowUtilizationStats(BaseModel):
    berth_id: int
    berth_name: str
    total_windows: int = 0
    used_windows: int = 0
    utilization_rate: float = 0.0
    period_start: datetime
    period_end: datetime


class DelayStats(BaseModel):
    total_tasks: int = 0
    delayed_tasks: int = 0
    delay_rate: float = 0.0
    average_delay_minutes: float = 0.0
    delay_reasons: List[dict] = []
    period_start: datetime
    period_end: datetime
