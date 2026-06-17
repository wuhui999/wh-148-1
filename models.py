from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, ForeignKey, Text, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum

from database import Base


class UserRole(str, enum.Enum):
    DISPATCHER = "dispatcher"
    ADMIN = "admin"
    PILOT = "pilot"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ShipType(str, enum.Enum):
    CONTAINER = "container"
    BULK = "bulk"
    TANKER = "tanker"
    PASSENGER = "passenger"
    GENERAL = "general"
    RORO = "roro"


class PilotQualification(str, enum.Enum):
    JUNIOR = "junior"
    SENIOR = "senior"
    MASTER = "master"


class VesselStatus(str, enum.Enum):
    AVAILABLE = "available"
    IN_MAINTENANCE = "maintenance"
    IN_USE = "in_use"


class AuditAction(str, enum.Enum):
    TASK_CREATED = "task_created"
    TASK_ASSIGNED = "task_assigned"
    TASK_REASSIGNED = "task_reassigned"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_CANCELLED = "task_cancelled"
    DELAY_RECORDED = "delay_recorded"
    DUTY_RULE_CREATED = "duty_rule_created"
    DUTY_RULE_UPDATED = "duty_rule_updated"
    DUTY_RULE_DELETED = "duty_rule_deleted"
    DUTY_VIOLATION_REJECTED = "duty_violation_rejected"


class ViolationType(str, enum.Enum):
    DAILY_TASK_LIMIT = "daily_task_limit"
    REST_INTERVAL = "rest_interval"
    CONSECUTIVE_WORK = "consecutive_work"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(Enum(UserRole), default=UserRole.DISPATCHER, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    audit_logs = relationship("AuditLog", back_populates="operator")


class Pilot(Base):
    __tablename__ = "pilots"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    license_number = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    phone = Column(String(20))
    qualification = Column(Enum(PilotQualification), nullable=False)
    certified_ship_types = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tasks = relationship("PilotTask", back_populates="pilot")
    user = relationship("User")


class Berth(Base):
    __tablename__ = "berths"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(20), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    max_draft = Column(Float, nullable=False)
    max_length = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tide_windows = relationship("TideWindow", back_populates="berth")


class Ship(Base):
    __tablename__ = "ships"

    id = Column(Integer, primary_key=True, index=True)
    imo_number = Column(String(20), unique=True, index=True)
    name = Column(String(100), nullable=False)
    ship_type = Column(Enum(ShipType), nullable=False)
    draft = Column(Float, nullable=False)
    length = Column(Float, nullable=False)
    flag = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tasks = relationship("PilotTask", back_populates="ship")


class TideWindow(Base):
    __tablename__ = "tide_windows"

    id = Column(Integer, primary_key=True, index=True)
    berth_id = Column(Integer, ForeignKey("berths.id"), nullable=False)
    window_start = Column(DateTime(timezone=True), nullable=False)
    window_end = Column(DateTime(timezone=True), nullable=False)
    min_safe_draft = Column(Float, nullable=False)
    tide_height = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    berth = relationship("Berth", back_populates="tide_windows")


class SafetyMarginConfig(Base):
    __tablename__ = "safety_margin_configs"

    id = Column(Integer, primary_key=True, index=True)
    ship_type = Column(Enum(ShipType), unique=True, nullable=False)
    draft_margin = Column(Float, default=0.3, nullable=False)
    time_margin_minutes = Column(Integer, default=30, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TransportVessel(Base):
    __tablename__ = "transport_vessels"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    capacity = Column(Integer, nullable=False)
    status = Column(Enum(VesselStatus), default=VesselStatus.AVAILABLE, nullable=False)
    available_from = Column(DateTime(timezone=True), nullable=True)
    available_to = Column(DateTime(timezone=True), nullable=True)
    maintenance_notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tasks = relationship("PilotTask", back_populates="vessel")


class PilotTask(Base):
    __tablename__ = "pilot_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_number = Column(String(30), unique=True, index=True, nullable=False)
    ship_id = Column(Integer, ForeignKey("ships.id"), nullable=False)
    berth_id = Column(Integer, ForeignKey("berths.id"), nullable=False)
    pilot_id = Column(Integer, ForeignKey("pilots.id"), nullable=True)
    vessel_id = Column(Integer, ForeignKey("transport_vessels.id"), nullable=True)
    tide_window_id = Column(Integer, ForeignKey("tide_windows.id"), nullable=True)
    task_type = Column(String(20), nullable=False)
    planned_boarding_time = Column(DateTime(timezone=True), nullable=False)
    status = Column(Enum(TaskStatus), default=TaskStatus.PENDING, nullable=False)
    boarding_point = Column(String(100))
    notes = Column(Text, nullable=True)
    actual_boarding_time = Column(DateTime(timezone=True), nullable=True)
    actual_completion_time = Column(DateTime(timezone=True), nullable=True)
    delay_minutes = Column(Integer, default=0)
    delay_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    ship = relationship("Ship", back_populates="tasks")
    berth = relationship("Berth")
    pilot = relationship("Pilot", back_populates="tasks")
    vessel = relationship("TransportVessel", back_populates="tasks")
    tide_window = relationship("TideWindow")
    audit_logs = relationship("AuditLog", back_populates="task")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("pilot_tasks.id"), nullable=False)
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(Enum(AuditAction), nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    remark = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    task = relationship("PilotTask", back_populates="audit_logs")
    operator = relationship("User", back_populates="audit_logs")


class PilotDutyRule(Base):
    __tablename__ = "pilot_duty_rules"

    id = Column(Integer, primary_key=True, index=True)
    pilot_id = Column(Integer, ForeignKey("pilots.id"), nullable=True, unique=True)
    max_tasks_per_day = Column(Integer, nullable=False, default=5)
    min_rest_minutes_between_tasks = Column(Integer, nullable=False, default=60)
    max_consecutive_work_minutes = Column(Integer, nullable=False, default=480)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    pilot = relationship("Pilot")


class DutyViolationLog(Base):
    __tablename__ = "duty_violation_logs"

    id = Column(Integer, primary_key=True, index=True)
    pilot_id = Column(Integer, ForeignKey("pilots.id"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("pilot_tasks.id"), nullable=True, index=True)
    violation_type = Column(Enum(ViolationType), nullable=False)
    violation_detail = Column(String(255), nullable=True)
    rejected_at = Column(DateTime(timezone=True), server_default=func.now())

    pilot = relationship("Pilot")
    task = relationship("PilotTask")
