"""Request bodies (Pydantic). Responses follow the shapes in the spec."""
from datetime import date, datetime
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PhoneObj(BaseModel):
    country: str = Field("IN", examples=["IN"])
    number: str = Field(..., examples=["9849011234"])


Phone = Union[PhoneObj, str]

AppName = Literal["admin", "member", "web", "console"]
Method = Literal["cash", "upi", "card", "bank"]
Gender = Literal["female", "male", "other"]


# ---------- auth ----------
class OtpSendIn(BaseModel):
    phone: Phone = Field(..., examples=[{"country": "IN", "number": "9849011234"}])
    app: AppName = Field(..., examples=["member"])


class DeviceIn(BaseModel):
    name: str | None = Field(None, examples=["Pixel 7"])
    platform: str | None = Field(None, examples=["android"])


class OtpVerifyIn(BaseModel):
    request_id: str
    code: str = Field(..., min_length=4, max_length=8, examples=["123456"])
    device: DeviceIn | None = None


class FirebaseLoginIn(BaseModel):
    id_token: str = Field(..., description="Firebase ID token from user.getIdToken() after phone sign-in")
    app: AppName = Field(..., examples=["member"])
    device: DeviceIn | None = None


class RefreshIn(BaseModel):
    refresh_token: str


class MePatch(StrictModel):
    name: str | None = Field(None, max_length=200)
    email: EmailStr | None = None
    dob: date | None = None
    gender: Gender | None = None
    photo_key: str | None = None
    weekly_goal: int | None = Field(None, ge=2, le=6)
    show_on_leaderboard: bool | None = None


class DeviceTokenIn(BaseModel):
    expo_push_token: str = Field(..., examples=["ExponentPushToken[xxxxxxxx]"])
    app: Literal["admin", "member"]
    platform: Literal["android", "ios", "web"]


# ---------- gyms ----------
class GymCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=200, examples=["Iron Republic Fitness · Kondapur"])
    owner_name: str | None = Field(None, examples=["Karthik Varma"])
    email: EmailStr | None = None
    phone: Phone | None = None
    address_line: str | None = None
    area: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = Field(None, pattern=r"^\d{6}$")
    code_prefix: str | None = Field(None, pattern=r"^[A-Za-z]{3}$", description="Optional; auto-suggested")


class DocumentIn(BaseModel):
    type: Literal["gst_certificate", "trade_licence", "owner_id"]
    file_key: str


class GymSettingsIn(BaseModel):
    checkin_cooldown_min: int | None = Field(None, ge=0, le=1440)
    checkin_static_qr: bool | None = Field(None, description="Allow the printable poster QR (else only the live screen QR)")
    checkin_require_location: bool | None = Field(None, description="Member must be near the gym to check in")
    checkin_radius_m: int | None = Field(None, ge=30, le=2000)
    capacity: int | None = Field(None, ge=1, le=5000, description="Max people inside at once — makes the crowd % exact")
    avg_session_min: int | None = Field(None, ge=20, le=240, description="Typical workout length; used by the crowd meter")
    max_freeze_days: int | None = Field(None, ge=0, le=365)
    receipt_footer: str | None = Field(None, max_length=300)


class GymPatch(StrictModel):
    name: str | None = Field(None, min_length=2, max_length=200)
    phone: Phone | None = None
    email: EmailStr | None = None
    address_line: str | None = None
    area: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = Field(None, pattern=r"^\d{6}$")
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    hours: dict[str, list[list[str]]] | None = Field(None, examples=[{"mon": [["05:00", "23:00"]]}])
    gstin: str | None = Field(None, pattern=r"^[0-9A-Z]{15}$")
    logo_key: str | None = None
    timezone: str | None = None
    settings: GymSettingsIn | None = None


class PlanExtras(BaseModel):
    offer_price_paise: int | None = Field(None, ge=0, description="When set, members pay this instead of price_paise")
    joining_fee_paise: int | None = Field(None, ge=0, description="Added to a member’s first membership at this gym")
    gst_mode: Literal["inclusive", "exclusive"] | None = Field(None, description="exclusive = GST added on top")
    gst_rate: int | None = Field(None, ge=0, le=28)
    facilities: list[str] | None = Field(None, examples=[["Cardio", "Weights", "Steam"]])
    description: str | None = None
    freeze_allowed: bool | None = None
    max_freeze_days: int | None = Field(None, ge=0, le=365, description="Overrides the gym’s max_freeze_days")
    cancellation_policy: str | None = None
    refund_policy: str | None = None


class PlanCreate(PlanExtras):
    name: str = Field(..., min_length=1, max_length=120, examples=["Quarterly"])
    duration_days: int = Field(..., ge=1, le=730, examples=[92])
    price_paise: int = Field(..., ge=0, examples=[540000])
    sort_order: int | None = None


class PlanPatch(PlanExtras):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=120)
    duration_days: int | None = Field(None, ge=1, le=730)
    price_paise: int | None = Field(None, ge=0)
    active: bool | None = None
    sort_order: int | None = None


# ---------- members ----------
class PaymentInline(BaseModel):
    amount_paise: int = Field(..., gt=0)
    method: Method
    reference: str | None = None
    paid_at: datetime | None = None


class MembershipInline(BaseModel):
    plan_id: str
    start_date: date | None = None
    discount_paise: int = Field(0, ge=0)


class MemberCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, examples=["Meghana Reddy"])
    phone: Phone
    email: EmailStr | None = None
    dob: date | None = None
    gender: Gender | None = None
    emergency_contact: str | None = None
    notes: str | None = None
    photo_key: str | None = None
    membership: MembershipInline | None = None
    payment: PaymentInline | None = None


class MemberPatch(StrictModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    email: EmailStr | None = None
    dob: date | None = None
    gender: Gender | None = None
    emergency_contact: str | None = None
    notes: str | None = None
    photo_key: str | None = None
    status: Literal["archived", "active"] | None = None
    phone: Phone | None = None


class MembershipCreate(BaseModel):
    plan_id: str
    start_date: date | None = None
    discount_paise: int = Field(0, ge=0)
    payment: PaymentInline | None = None


class FreezeIn(BaseModel):
    from_date: date | None = Field(None, description="Defaults to today")
    days: int | None = Field(None, ge=1, le=365, description="Omit for an open-ended freeze")
    reason: Literal["travel", "medical", "other"]
    note: str | None = None


class SuspendIn(BaseModel):
    reason: str = Field(..., min_length=1, examples=["Payment pending / misconduct"])


class NoteIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)


class CancelIn(BaseModel):
    reason: str = Field(..., min_length=1)
    refund_note: str | None = None


# ---------- payments ----------
class PaymentCreate(BaseModel):
    member_id: str
    membership_id: str | None = None
    amount_paise: int = Field(..., gt=0)
    method: Method
    reference: str | None = None
    paid_at: datetime | None = None
    note: str | None = None
    confirm_duplicate: bool = False
    as_credit: bool = False


class VoidIn(BaseModel):
    reason: str = Field(..., min_length=1, examples=["Entered twice"])


# ---------- check-ins ----------
class ScanIn(BaseModel):
    token: str = Field(..., examples=["DBF1.eyJ..."])
    device_id: str | None = None


class ManualCheckinIn(BaseModel):
    member_id: str
    note: str | None = None


# ---------- imports ----------
class ImportFromKey(BaseModel):
    file_key: str
    file_name: str | None = None


class MappingIn(BaseModel):
    mapping: dict[str, str | None] = Field(..., examples=[{"Package": "plan", "Locker No": None}])


class ImportRowPatch(StrictModel):
    phone: Phone | None = None
    name: str | None = None
    plan_id: str | None = None
    custom_plan: bool | None = Field(None, description="Import with the file’s dates as a custom plan")
    start_date: date | None = None
    end_date: date | None = None
    paid_paise: int | None = Field(None, ge=0)
    email: str | None = None
    skip: bool | None = None


# ---------- staff ----------
StaffRole = Literal["manager", "receptionist", "trainer", "staff"]


class StaffCreate(BaseModel):
    name: str = Field(..., min_length=1)
    phone: Phone
    staff_role: StaffRole = "staff"
    permissions: list[str] | None = Field(None, description="Optional explicit permission list (overrides the role preset)")
    can_record_payments: bool = False
    branch_ids: list[str] | None = Field(None, description="Also give access to these branches of the same business")


class StaffPatch(StrictModel):
    staff_role: StaffRole | None = None
    permissions: list[str] | None = None
    can_record_payments: bool | None = None
    status: Literal["deactivated", "active"] | None = None


# ---------- member app ----------
class DisputeIn(BaseModel):
    note: str | None = None


# ---------- internal ----------
class ReasonIn(BaseModel):
    reason: str | None = None


class AdminIn(BaseModel):
    phone: Phone
    name: str | None = None


class ChangePhoneIn(BaseModel):
    new_phone: Phone
    ticket_ref: str = Field(..., min_length=1)


class PresignIn(BaseModel):
    purpose: Literal["member_photo", "gym_logo", "gym_document", "import_file"]
    content_type: str = Field(..., examples=["image/jpeg"])
    size: int = Field(..., gt=0, examples=[123456])
