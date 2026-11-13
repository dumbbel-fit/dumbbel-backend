"""Response schemas for Swagger / the frontend. Every model allows extra fields and routes use
response_model_exclude_unset=True, so fields an endpoint doesn't set (e.g. money for staff without
payments.view) are omitted rather than returned as null."""
from typing import Any

from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict


class Out(BaseModel):
    model_config = ConfigDict(extra="allow")


class DocRoute(APIRoute):
    def __init__(self, *args, **kwargs):
        # APIRouter always passes this flag explicitly (False), so force it.
        kwargs["response_model_exclude_unset"] = True
        super().__init__(*args, **kwargs)


# ---------- errors ----------
class ErrorBody(Out):
    code: str
    message: str
    details: dict[str, Any] | None = None
    request_id: str | None = None


class ErrorOut(Out):
    error: ErrorBody


ERRORS = {s: {"model": ErrorOut, "description": d} for s, d in [
    (400, "Bad request"), (401, "Not logged in / token expired"), (403, "Not allowed for this role"),
    (404, "Not found"), (409, "Conflict"), (422, "Validation error (details.fields)"), (423, "Gym pending/suspended"),
    (429, "Rate limited (Retry-After header)")]}


# ---------- common ----------
class OkOut(Out):
    ok: bool | None = None
    status: str | None = None


class HealthOut(Out):
    status: str
    db: bool
    time: str
    db_error: str | None = None


class FirebaseWebConfig(Out):
    apiKey: str | None = None
    authDomain: str | None = None
    projectId: str | None = None
    appId: str | None = None
    messagingSenderId: str | None = None
    storageBucket: str | None = None


class ConfigOut(Out):
    min_app_version: dict[str, str]
    support_phone: str
    maintenance: bool
    auth_provider: str
    firebase_project_id: str | None = None
    firebase: FirebaseWebConfig | None = None


# ---------- auth / me ----------
class UserOut(Out):
    id: str
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    dob: str | None = None
    gender: str | None = None
    photo_url: str | None = None
    weekly_goal: int | None = None
    first_login_at: str | None = None
    deletion_requested_at: str | None = None
    is_new: bool | None = None
    is_platform_admin: bool | None = None


class StaffOfOut(Out):
    gym_id: str
    gym_name: str
    gym_status: str
    status_reason: str | None = None
    org_id: str | None = None
    role: str
    staff_role: str | None = None
    can_record_payments: bool
    permissions: list[str] | None = None


class OtpSendOut(Out):
    request_id: str
    resend_after_sec: int
    expires_in_sec: int


class TokensOut(Out):
    access_token: str
    refresh_token: str
    expires_in_sec: int


class CurrentOut(Out):
    id: str | None = None
    membership_id: str | None = None
    plan_name: str | None = None
    status: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    days_left: int | None = None
    due_paise: int | None = None


class MemberCardOut(Out):
    member_id: str
    member_code: str | None = None
    gym: dict[str, Any] | None = None
    current: CurrentOut | None = None
    due_paise: int | None = None
    stats: dict[str, Any] | None = None


class ClaimOut(Out):
    member_id: str
    gym_id: str | None = None
    gym_name: str | None = None
    member_code: str | None = None
    plan_name: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class LoginOut(TokensOut):
    user: UserOut
    staff_of: list[StaffOfOut] | None = None
    memberships_summary: list[MemberCardOut] | None = None
    new_claims: list[ClaimOut] | None = None


class MeOut(Out):
    user: UserOut
    staff_of: list[StaffOfOut]
    weekly_goal: int | None = None


class UserWrapOut(Out):
    user: UserOut


class DeletionOut(Out):
    deletion_scheduled_for: str


# ---------- gym ----------
class GymOut(Out):
    id: str
    name: str
    code_prefix: str | None = None
    status: str | None = None
    status_reason: str | None = None
    org_id: str | None = None
    owner_user_id: str | None = None
    phone: str | None = None
    email: str | None = None
    address_line: str | None = None
    area: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    lat: float | None = None
    lng: float | None = None
    hours: dict[str, Any] | None = None
    timezone: str | None = None
    gstin: str | None = None
    logo_url: str | None = None
    settings: dict[str, Any] | None = None
    approved_at: str | None = None
    created_at: str | None = None
    me: dict[str, Any] | None = None
    owner: dict[str, Any] | None = None
    counts: dict[str, Any] | None = None
    documents: list[dict[str, Any]] | None = None


class GymListOut(Out):
    items: list[GymOut]
    next_cursor: str | None = None


class DocumentOut(Out):
    id: str
    type: str
    status: str
    created_at: str | None = None


class VerificationOut(Out):
    status: str
    steps: list[dict[str, Any]]
    reason: str | None = None


class PlanOut(Out):
    id: str
    name: str
    duration_days: int
    price_paise: int
    offer_price_paise: int | None = None
    effective_price_paise: int | None = None
    joining_fee_paise: int | None = None
    gst_mode: str | None = None
    gst_rate: int | None = None
    facilities: list[str] | None = None
    description: str | None = None
    freeze_allowed: bool | None = None
    max_freeze_days: int | None = None
    cancellation_policy: str | None = None
    refund_policy: str | None = None
    active: bool
    sort_order: int | None = None
    active_members: int | None = None


class PlanListOut(Out):
    items: list[PlanOut]


# ---------- members ----------
class MemberListItemOut(Out):
    id: str
    member_code: str
    name: str
    phone: str | None = None
    photo_url: str | None = None
    on_app: bool | None = None
    status: str | None = None
    source: str | None = None
    joined_at: str | None = None
    last_checkin_at: str | None = None
    last_visit_at: str | None = None
    current: CurrentOut | None = None
    current_membership: CurrentOut | None = None
    has_dues: bool | None = None
    due_paise: int | None = None


class MemberListOut(Out):
    items: list[MemberListItemOut]
    next_cursor: str | None = None
    counts: dict[str, int]
    total: int | None = None


class MemberOut(Out):
    id: str
    member_code: str
    name: str
    phone: str | None = None
    email: str | None = None
    dob: str | None = None
    gender: str | None = None
    photo_url: str | None = None
    emergency_contact: str | None = None
    notes: str | None = None
    status: str | None = None
    source: str | None = None
    joined_on: str | None = None
    joined_at: str | None = None
    last_checkin_at: str | None = None
    last_visit_at: str | None = None
    on_app: bool | None = None
    claimed_at: str | None = None
    claim_disputed_at: str | None = None
    created_at: str | None = None
    current_membership: CurrentOut | None = None


class MembershipOut(Out):
    id: str
    member_id: str | None = None
    plan_id: str | None = None
    plan_name: str
    start_date: str
    end_date: str
    state: str | None = None
    status: str
    days_left: int | None = None
    source: str | None = None
    price_paise: int | None = None
    discount_paise: int | None = None
    joining_fee_paise: int | None = None
    gst_paise: int | None = None
    due_paise: int | None = None
    cancelled_at: str | None = None
    cancel_reason: str | None = None
    suspended_at: str | None = None
    suspend_reason: str | None = None
    freezes: list[dict[str, Any]] | None = None
    created_at: str | None = None


class PaymentOut(Out):
    id: str
    receipt_no: str
    member: dict[str, Any] | None = None
    membership: dict[str, Any] | None = None
    amount_paise: int
    method: str
    reference: str | None = None
    paid_at: str
    source: str | None = None
    status: str
    void_reason: str | None = None
    voided_at: str | None = None
    recorded_by: dict[str, Any] | None = None
    note: str | None = None


class MemberCreateOut(Out):
    member: MemberOut
    membership: MembershipOut | None = None
    payment: PaymentOut | None = None


class MemberWrapOut(Out):
    member: MemberOut


class MemberDetailOut(Out):
    member: MemberOut
    user: dict[str, Any] | None = None
    current_membership: MembershipOut | None = None
    memberships: list[MembershipOut]
    payments: list[PaymentOut] | None = None
    due_paise: int | None = None
    credit_paise: int | None = None
    stats: dict[str, Any] | None = None
    insights: list[dict[str, Any]] | None = None
    notes_history: list[dict[str, Any]] | None = None


class CheckinItemOut(Out):
    id: str | None = None
    at: str
    method: str | None = None
    result: str | None = None
    staff_name: str | None = None


class MemberCheckinsOut(Out):
    month: str
    days: list[int]
    items: list[CheckinItemOut]


class MembershipCreateOut(Out):
    membership: MembershipOut
    payment: PaymentOut | None = None
    due_paise: int | None = None


class MembershipWrapOut(Out):
    membership: MembershipOut


class NoteOut(Out):
    id: str
    body: str
    author: dict[str, Any] | None = None
    created_at: str


class NoteListOut(Out):
    items: list[NoteOut]


# ---------- payments ----------
class PaymentListOut(Out):
    items: list[PaymentOut]
    totals: dict[str, Any]
    next_cursor: str | None = None


class PaymentCreateOut(Out):
    payment: PaymentOut
    credit_payment: PaymentOut | None = None
    due_paise: int | None = None


class PaymentWrapOut(Out):
    payment: PaymentOut


class ReceiptOut(Out):
    id: str
    receipt_no: str
    status: str
    gym: dict[str, Any]
    member: dict[str, Any]
    plan_name: str | None = None
    amount_paise: int
    amount_in_words: str
    method: str
    reference: str | None = None
    paid_at: str
    paid_at_local: str | None = None
    recorded_by: str | None = None
    footer: str | None = None


class PaymentStatsPoint(Out):
    period: str
    start: str
    collected_paise: int
    count: int
    by_method: dict[str, int]
    by_source: dict[str, int]


class PaymentStatsOut(Out):
    range: str
    group: str
    from_: str | None = None
    to: str | None = None
    series: list[PaymentStatsPoint]
    totals: dict[str, Any]


# ---------- check-ins ----------
class CheckinResultOut(Out):
    result: str
    message: str | None = None
    checkin_id: str
    at: str
    method: str | None = None
    member: dict[str, Any] | None = None
    membership: dict[str, Any] | None = None
    previous_at: str | None = None
    streak: int | None = None
    due_paise: int | None = None


class CheckinFeedOut(Out):
    date: str
    items: list[dict[str, Any]]
    counts: dict[str, int]
    server_time: str | None = None


class CheckinStatsOut(Out):
    range: str
    total: int
    unique_members: int
    denied: int
    by_result: dict[str, int]
    by_hour: list[int]
    by_day: list[dict[str, Any]]
    peak_hour: int | None = None


# ---------- import ----------
class ImportUploadOut(Out):
    import_id: str
    status: str
    rows: int | None = None


class ImportOut(Out):
    id: str
    status: str
    file_name: str | None = None
    counts: dict[str, Any] | None = None
    progress: float | None = None
    processed: int | None = None
    total: int | None = None
    columns: list[dict[str, Any]] | None = None
    mapping: dict[str, Any] | None = None
    error: str | None = None
    created_at: str | None = None
    committed_at: str | None = None
    completed_at: str | None = None


class ImportListOut(Out):
    items: list[ImportOut]
    next_cursor: str | None = None


class ImportRowOut(Out):
    id: str
    row_number: int
    raw: dict[str, Any] | None = None
    normalized: dict[str, Any] | None = None
    outcome: str
    issues: list[dict[str, Any]] | None = None
    suggestion: dict[str, Any] | None = None
    member_id: str | None = None
    existing_member: dict[str, Any] | None = None


class ImportRowsOut(Out):
    items: list[ImportRowOut]
    next_cursor: str | None = None


class ImportRowPatchOut(Out):
    row: ImportRowOut
    counts: dict[str, Any] | None = None


class MappingOut(Out):
    counts: dict[str, Any]
    columns: list[dict[str, Any]]


# ---------- staff / home ----------
class StaffOut(Out):
    id: str
    user: dict[str, Any]
    role: str
    staff_role: str | None = None
    permissions: list[str] | None = None
    can_record_payments: bool
    status: str
    branches: list[dict[str, Any]] | None = None
    invited_at: str | None = None
    joined_at: str | None = None
    deactivated_at: str | None = None
    last_active_at: str | None = None
    last_action: dict[str, Any] | None = None
    logged_in: bool | None = None


class StaffListOut(Out):
    items: list[StaffOut]


class RolesOut(Out):
    roles: dict[str, list[str]]
    permissions: dict[str, str]


class SummaryOut(Out):
    date: str
    gym: dict[str, Any] | None = None
    numbers: dict[str, Any]
    insights: list[dict[str, Any]]
    expiring: list[dict[str, Any]]


class RemindersOut(Out):
    type: str
    items: list[dict[str, Any]]


class ActivityOut(Out):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


# ---------- member app ----------
class MyMembershipsOut(Out):
    items: list[MemberCardOut]


class QrOut(Out):
    member_id: str
    token: str | None = None
    expires_at: str | None = None
    refresh_in_sec: int
    status: str
    message: str | None = None
    days_left: int | None = None
    gym: dict[str, Any] | None = None


class MyCheckinsOut(Out):
    month: str
    days: list[int]
    items: list[dict[str, Any]]
    totals: dict[str, Any]


class InsightsOut(Out):
    items: list[dict[str, Any]]


class MyPaymentsOut(Out):
    due_paise: int
    credit_paise: int | None = None
    items: list[dict[str, Any]]


# ---------- files / internal ----------
class PresignOut(Out):
    upload_url: str
    file_key: str
    expires_in_sec: int
    method: str | None = None
    headers: dict[str, str] | None = None


class AdminsOut(Out):
    items: list[UserOut]


# ---------- UPI ----------
class UpiAccountOut(Out):
    id: str
    vpa: str
    payee_name: str
    label: str | None = None
    is_default: bool
    active: bool
    created_at: str | None = None


class UpiAccountListOut(Out):
    items: list[UpiAccountOut]


class UpiRequestOut(Out):
    id: str
    status: str
    amount_paise: int
    vpa: str
    payee_name: str
    ref: str
    note: str | None = None
    upi_uri: str | None = None
    app_links: dict[str, str] | None = None
    member: dict[str, Any] | None = None
    membership_id: str | None = None
    utr: str | None = None
    claimed_at: str | None = None
    decided_at: str | None = None
    reject_reason: str | None = None
    payment_id: str | None = None
    receipt_no: str | None = None
    created_via: str | None = None
    expires_at: str | None = None
    created_at: str | None = None


class UpiRequestListOut(Out):
    items: list[UpiRequestOut]
    next_cursor: str | None = None
    counts: dict[str, int] | None = None


class MyUpiInfoOut(Out):
    member_id: str
    gym: dict[str, Any]
    upi_enabled: bool
    payee_name: str | None = None
    vpa: str | None = None
    due_paise: int
    open_request: UpiRequestOut | None = None
