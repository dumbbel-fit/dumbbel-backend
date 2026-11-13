"""All tables from spec §3 (MySQL 8 flavour) + idempotency_keys.

MySQL has no partial indexes, so the spec's partial-unique rules are enforced with
STORED generated columns that are NULL when the rule doesn't apply (UNIQUE ignores NULLs):
  * plans: UNIQUE(gym_id, lower(name)) among active          -> active_name_key
  * payments: UNIQUE(gym_id, method, reference) while recorded -> ref_guard
  * gym_staff: exactly one owner per gym                       -> owner_guard
"""
from datetime import timezone

from sqlalchemy import (JSON, BigInteger, Boolean, Computed, Date, Enum, ForeignKey, Index, Integer, Numeric,
                        SmallInteger, String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.db import Base
from app.utils import uuid7


class UTCDateTime(TypeDecorator):
    """Stores naive UTC DATETIME(6); always returns tz-aware UTC."""
    impl = DATETIME(fsp=6)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def pk():
    return mapped_column(String(36), primary_key=True, default=uuid7)


def fk(target: str, nullable=False, index=False, ondelete=None):
    return mapped_column(String(36), ForeignKey(target, ondelete=ondelete), nullable=nullable, index=index)


def created():
    return mapped_column(UTCDateTime, server_default=func.now(6), nullable=False)


def updated():
    return mapped_column(UTCDateTime, server_default=func.now(6), onupdate=func.now(6), nullable=False)


TS = UTCDateTime

APP_ENUM = ("admin", "member", "web", "console")


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = pk()
    phone: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(254))
    dob = mapped_column(Date, nullable=True)
    gender = mapped_column(Enum("female", "male", "other", name="gender"), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(500))
    weekly_goal: Mapped[int] = mapped_column(SmallInteger, default=4, server_default="4", nullable=False)
    first_login_at = mapped_column(TS, nullable=True)
    last_login_at = mapped_column(TS, nullable=True)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    deletion_requested_at = mapped_column(TS, nullable=True)
    deleted_at = mapped_column(TS, nullable=True)
    created_at = created()
    updated_at = updated()


class OtpCode(Base):
    __tablename__ = "otp_codes"
    id: Mapped[str] = pk()
    phone: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    app = mapped_column(Enum(*APP_ENUM, name="otp_app"), nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0", nullable=False)
    expires_at = mapped_column(TS, nullable=False)
    consumed_at = mapped_column(TS, nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(String(100))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    created_at = created()
    __table_args__ = (Index("ix_otp_phone_created", "phone", "created_at"),)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = pk()
    user_id: Mapped[str] = fk("users.id", index=True)
    app = mapped_column(Enum(*APP_ENUM, name="session_app"), nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    device_name: Mapped[str | None] = mapped_column(String(200))
    platform: Mapped[str | None] = mapped_column(String(100))
    expires_at = mapped_column(TS, nullable=False)
    revoked_at = mapped_column(TS, nullable=True)
    last_used_at = mapped_column(TS, nullable=True)
    created_at = created()
    updated_at = updated()


class Gym(Base):
    __tablename__ = "gyms"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code_prefix: Mapped[str] = mapped_column(String(3), unique=True, nullable=False)
    owner_user_id: Mapped[str] = fk("users.id", index=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(254))
    address_line: Mapped[str | None] = mapped_column(String(300))
    area: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    pincode: Mapped[str | None] = mapped_column(String(10))
    lat = mapped_column(Numeric(9, 6), nullable=True)
    lng = mapped_column(Numeric(9, 6), nullable=True)
    hours = mapped_column(JSON, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", server_default="Asia/Kolkata", nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    logo_url: Mapped[str | None] = mapped_column(String(500))
    status = mapped_column(Enum("pending", "active", "rejected", "suspended", name="gym_status"),
                           default="pending", server_default="pending", nullable=False, index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    approved_at = mapped_column(TS, nullable=True)
    approved_by: Mapped[str | None] = fk("users.id", nullable=True)
    settings = mapped_column(JSON, nullable=True)
    org_id: Mapped[str | None] = mapped_column(String(36), index=True)  # branches of one business share an org_id
    member_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    receipt_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_at = created()
    updated_at = updated()


class GymDocument(Base):
    __tablename__ = "gym_documents"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id", index=True)
    type = mapped_column(Enum("gst_certificate", "trade_licence", "owner_id", name="doc_type"), nullable=False)
    file_key: Mapped[str] = mapped_column(String(500), nullable=False)
    status = mapped_column(Enum("submitted", "accepted", "rejected", name="doc_status"),
                           default="submitted", server_default="submitted", nullable=False)
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = fk("users.id", nullable=True)
    reviewed_at = mapped_column(TS, nullable=True)
    created_at = created()
    updated_at = updated()


class GymStaff(Base):
    __tablename__ = "gym_staff"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id")
    user_id: Mapped[str] = fk("users.id", index=True)
    role = mapped_column(Enum("owner", "staff", name="staff_role"), nullable=False)
    can_record_payments: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    status = mapped_column(Enum("invited", "active", "deactivated", name="staff_status"),
                           default="invited", server_default="invited", nullable=False)
    invited_by: Mapped[str | None] = fk("users.id", nullable=True)
    invited_at = mapped_column(TS, nullable=True)
    joined_at = mapped_column(TS, nullable=True)
    deactivated_at = mapped_column(TS, nullable=True)
    staff_role: Mapped[str | None] = mapped_column(String(20), server_default="staff")  # manager|receptionist|trainer|staff
    permissions = mapped_column(JSON, nullable=True)  # explicit permission list; overrides the role preset
    owner_guard = mapped_column(SmallInteger, Computed("IF(role = 'owner', 1, NULL)", persisted=True))
    created_at = created()
    updated_at = updated()
    __table_args__ = (UniqueConstraint("gym_id", "user_id", name="uq_staff_gym_user"),
                      UniqueConstraint("gym_id", "owner_guard", name="uq_staff_one_owner"))


class GymMember(Base):
    __tablename__ = "gym_members"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id")
    user_id: Mapped[str] = fk("users.id", index=True)
    member_code: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source = mapped_column(Enum("manual", "import", "app", name="member_source"), default="manual", nullable=False)
    import_id: Mapped[str | None] = fk("imports.id", nullable=True)
    emergency_contact: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    status = mapped_column(Enum("active", "archived", name="member_status"),
                           default="active", server_default="active", nullable=False)
    joined_on = mapped_column(Date, nullable=False)
    last_checkin_at = mapped_column(TS, nullable=True)
    best_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)  # cache (§4)
    claimed_at = mapped_column(TS, nullable=True)
    claim_disputed_at = mapped_column(TS, nullable=True)
    claim_dispute_note: Mapped[str | None] = mapped_column(Text)
    created_at = created()
    updated_at = updated()
    __table_args__ = (UniqueConstraint("gym_id", "user_id", name="uq_member_gym_user"),
                      UniqueConstraint("gym_id", "member_code", name="uq_member_code"),
                      Index("ix_member_gym_status", "gym_id", "status"),
                      Index("ix_member_gym_lastcheckin", "gym_id", "last_checkin_at"),
                      Index("ix_member_gym_name", "gym_id", "display_name"))


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id", index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    offer_price_paise: Mapped[int | None] = mapped_column(Integer)  # when set, this is what members pay
    joining_fee_paise: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    gst_mode: Mapped[str] = mapped_column(String(10), default="inclusive", server_default="inclusive", nullable=False)
    gst_rate: Mapped[int] = mapped_column(SmallInteger, default=18, server_default="18", nullable=False)
    facilities = mapped_column(JSON, nullable=True)
    description: Mapped[str | None] = mapped_column(Text)
    freeze_allowed: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    max_freeze_days: Mapped[int | None] = mapped_column(Integer)  # per-plan override of the gym setting
    cancellation_policy: Mapped[str | None] = mapped_column(Text)
    refund_policy: Mapped[str | None] = mapped_column(Text)
    active_name_key = mapped_column(String(120), Computed("IF(active, LOWER(name), NULL)", persisted=True))
    created_at = created()
    updated_at = updated()
    __table_args__ = (UniqueConstraint("gym_id", "active_name_key", name="uq_plan_active_name"),)


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id")
    member_id: Mapped[str] = fk("gym_members.id", index=True)
    plan_id: Mapped[str | None] = fk("plans.id", nullable=True, index=True)
    plan_name: Mapped[str] = mapped_column(String(120), nullable=False)
    price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_paise: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    start_date = mapped_column(Date, nullable=False)
    end_date = mapped_column(Date, nullable=False)
    state = mapped_column(Enum("scheduled", "active", "frozen", "cancelled", name="membership_state"),
                          default="active", nullable=False)
    cancelled_at = mapped_column(TS, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    refund_note: Mapped[str | None] = mapped_column(Text)
    source = mapped_column(Enum("manual", "import", name="membership_source"), default="manual", nullable=False)
    created_by: Mapped[str | None] = fk("users.id", nullable=True)
    joining_fee_paise: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    gst_paise: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    suspended_at = mapped_column(TS, nullable=True)  # access paused, days keep running
    suspended_by: Mapped[str | None] = mapped_column(String(36))
    suspend_reason: Mapped[str | None] = mapped_column(Text)
    created_at = created()
    updated_at = updated()
    __table_args__ = (Index("ix_ms_gym_end", "gym_id", "end_date"),)


class MembershipFreeze(Base):
    __tablename__ = "membership_freezes"
    id: Mapped[str] = pk()
    membership_id: Mapped[str] = fk("memberships.id", index=True)
    from_date = mapped_column(Date, nullable=False)
    to_date = mapped_column(Date, nullable=True)
    days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reason = mapped_column(Enum("travel", "medical", "other", name="freeze_reason"), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = fk("users.id", nullable=True)
    ended_by: Mapped[str | None] = fk("users.id", nullable=True)
    ended_at = mapped_column(TS, nullable=True)
    created_at = created()
    updated_at = updated()


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id")
    member_id: Mapped[str] = fk("gym_members.id", index=True)
    membership_id: Mapped[str | None] = fk("memberships.id", nullable=True, index=True)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    method = mapped_column(Enum("cash", "upi", "card", "bank", name="pay_method"), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(100))
    paid_at = mapped_column(TS, nullable=False)
    source = mapped_column(Enum("manual", "import", "app", name="pay_source"), default="manual", nullable=False)
    receipt_no: Mapped[str] = mapped_column(String(40), nullable=False)
    status = mapped_column(Enum("recorded", "voided", name="pay_status"), default="recorded", nullable=False)
    void_reason: Mapped[str | None] = mapped_column(Text)
    voided_by: Mapped[str | None] = fk("users.id", nullable=True)
    voided_at = mapped_column(TS, nullable=True)
    recorded_by: Mapped[str | None] = fk("users.id", nullable=True)
    note: Mapped[str | None] = mapped_column(Text)
    ref_guard = mapped_column(String(120), Computed(
        "IF(status = 'recorded' AND reference IS NOT NULL, CONCAT(method, ':', reference), NULL)", persisted=True))
    created_at = created()
    updated_at = updated()
    __table_args__ = (UniqueConstraint("gym_id", "receipt_no", name="uq_receipt"),
                      UniqueConstraint("gym_id", "ref_guard", name="uq_payment_reference"),
                      Index("ix_pay_gym_paid", "gym_id", "paid_at"))


class Checkin(Base):
    __tablename__ = "checkins"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id")
    member_id: Mapped[str | None] = fk("gym_members.id", nullable=True)
    membership_id: Mapped[str | None] = fk("memberships.id", nullable=True)
    at = mapped_column(TS, nullable=False)
    local_date = mapped_column(Date, nullable=False)
    method = mapped_column(Enum("qr", "manual", name="checkin_method"), nullable=False)
    result = mapped_column(Enum("success", "duplicate", "expired", "frozen", "cancelled", "not_started",
                                "not_member", "invalid_token", "gym_inactive", "suspended",
                                name="checkin_result"), nullable=False)
    staff_user_id: Mapped[str | None] = fk("users.id", nullable=True)
    token_jti: Mapped[str | None] = mapped_column(String(64))
    device_id: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)
    created_at = created()
    __table_args__ = (Index("ix_ci_gym_at", "gym_id", "at"),
                      Index("ix_ci_member_date", "member_id", "local_date"),
                      Index("ix_ci_gym_date_result", "gym_id", "local_date", "result"))


class Import(Base):
    __tablename__ = "imports"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id", index=True)
    created_by: Mapped[str | None] = fk("users.id", nullable=True)
    file_key: Mapped[str | None] = mapped_column(String(500))
    file_name: Mapped[str | None] = mapped_column(String(300))
    file_size: Mapped[int | None] = mapped_column(Integer)
    status = mapped_column(Enum("uploaded", "parsing", "previewed", "committing", "completed", "failed", "cancelled",
                                name="import_status"), default="uploaded", nullable=False)
    headers = mapped_column(JSON, nullable=True)
    mapping = mapped_column(JSON, nullable=True)
    column_info = mapped_column(JSON, nullable=True)
    counts = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    committed_at = mapped_column(TS, nullable=True)
    completed_at = mapped_column(TS, nullable=True)
    created_at = created()
    updated_at = updated()


class ImportRow(Base):
    __tablename__ = "import_rows"
    id: Mapped[str] = pk()
    import_id: Mapped[str] = fk("imports.id", ondelete="CASCADE")
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw = mapped_column(JSON, nullable=False)
    normalized = mapped_column(JSON, nullable=True)
    overrides = mapped_column(JSON, nullable=True)  # owner edits from PATCH row; survive re-mapping
    outcome = mapped_column(Enum("ready", "existing", "fix", "skipped", "imported", "failed", name="import_outcome"),
                            nullable=False, default="fix")
    issues = mapped_column(JSON, nullable=True)
    suggestion = mapped_column(JSON, nullable=True)
    member_id: Mapped[str | None] = fk("gym_members.id", nullable=True)
    created_at = created()
    updated_at = updated()
    __table_args__ = (Index("ix_ir_import_outcome", "import_id", "outcome", "row_number"),)


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id")
    member_id: Mapped[str | None] = fk("gym_members.id", nullable=True)
    staff_id: Mapped[str | None] = fk("gym_staff.id", nullable=True)
    kind = mapped_column(Enum("member_app", "staff", name="invite_kind"), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    status = mapped_column(Enum("queued", "sent", "failed", "joined", name="invite_status"), default="queued", nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(36), index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[str | None] = mapped_column(Text)
    sent_at = mapped_column(TS, nullable=True)
    joined_at = mapped_column(TS, nullable=True)
    created_at = created()
    updated_at = updated()
    __table_args__ = (Index("ix_inv_member_created", "member_id", "created_at"),
                      Index("ix_inv_gym_phone", "gym_id", "phone"))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gym_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(36))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    before = mapped_column(JSON, nullable=True)
    after = mapped_column(JSON, nullable=True)
    at = mapped_column(TS, server_default=func.now(6), nullable=False)
    created_at = created()
    __table_args__ = (Index("ix_audit_gym_id", "gym_id", "id"),
                      Index("ix_audit_actor", "gym_id", "actor_user_id", "id"))


class DeviceToken(Base):
    __tablename__ = "device_tokens"
    id: Mapped[str] = pk()
    user_id: Mapped[str] = fk("users.id", index=True)
    app = mapped_column(Enum("admin", "member", name="device_app"), nullable=False)
    platform = mapped_column(Enum("android", "ios", "web", name="device_platform"), nullable=False)
    expo_push_token: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    last_seen_at = mapped_column(TS, nullable=True)
    created_at = created()
    updated_at = updated()


class IdempotencyKey(Base):
    """Idempotency-Key store (spec §1): same key within 24 h returns the original response."""
    __tablename__ = "idempotency_keys"
    id: Mapped[str] = pk()
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    route: Mapped[str] = mapped_column(String(200), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response = mapped_column(JSON, nullable=False)
    created_at = created()
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_idem_user_key"),)


class MemberNote(Base):
    """Dated note history on a member (staff only, never shown to the member)."""
    __tablename__ = "member_notes"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id")
    member_id: Mapped[str] = fk("gym_members.id", index=True)
    author_id: Mapped[str | None] = fk("users.id", nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at = mapped_column(TS, nullable=True)
    created_at = created()
    updated_at = updated()


class GymUpiAccount(Base):
    """The gym's own UPI IDs; members pay straight to the gym (0% fees, no aggregator)."""
    __tablename__ = "gym_upi_accounts"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id", index=True)
    vpa: Mapped[str] = mapped_column(String(120), nullable=False)
    payee_name: Mapped[str] = mapped_column(String(120), nullable=False)
    label: Mapped[str | None] = mapped_column(String(60))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    created_by: Mapped[str | None] = fk("users.id", nullable=True)
    created_at = created()
    updated_at = updated()
    __table_args__ = (UniqueConstraint("gym_id", "vpa", name="uq_gym_vpa"),)


class UpiPaymentRequest(Base):
    """A UPI collection: QR / intent with exact amount -> member submits UTR -> staff verify -> Payment recorded.
    A UTR is a claim, not proof: staff must confirm the credit in their bank app (see YouPI-PG guidance)."""
    __tablename__ = "upi_payment_requests"
    id: Mapped[str] = pk()
    gym_id: Mapped[str] = fk("gyms.id")
    member_id: Mapped[str] = fk("gym_members.id", index=True)
    membership_id: Mapped[str | None] = fk("memberships.id", nullable=True)
    upi_account_id: Mapped[str] = fk("gym_upi_accounts.id")
    vpa: Mapped[str] = mapped_column(String(120), nullable=False)  # snapshot
    payee_name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    ref: Mapped[str] = mapped_column(String(35), nullable=False, unique=True)  # UPI tr
    note: Mapped[str | None] = mapped_column(String(80))
    status = mapped_column(Enum("pending", "claimed", "verified", "rejected", "expired", "cancelled",
                                name="upi_req_status"), default="pending", nullable=False)
    created_via = mapped_column(Enum("staff", "member", name="upi_req_via"), nullable=False)
    created_by: Mapped[str | None] = fk("users.id", nullable=True)
    utr: Mapped[str | None] = mapped_column(String(40))
    claimed_at = mapped_column(TS, nullable=True)
    decided_by: Mapped[str | None] = fk("users.id", nullable=True)
    decided_at = mapped_column(TS, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    payment_id: Mapped[str | None] = fk("payments.id", nullable=True)
    expires_at = mapped_column(TS, nullable=False)
    created_at = created()
    updated_at = updated()
    __table_args__ = (Index("ix_upi_gym_status", "gym_id", "status", "created_at"),)
