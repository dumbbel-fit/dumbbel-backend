"""Roles & permissions. Owner has everything. Other staff get a role preset, optionally overridden by an explicit list.
`can_record_payments` (legacy switch) adds payments.view + payments.record to any role."""

PERMISSIONS = {
    "members.view": "See members",
    "members.edit": "Add and edit members, notes",
    "members.archive": "Archive / restore members",
    "memberships.manage": "Assign, renew, freeze, suspend memberships",
    "memberships.cancel": "Cancel memberships",
    "payments.view": "See money: payments, dues, collections",
    "payments.record": "Record payments, verify UPI payments",
    "payments.void": "Void payments",
    "checkins.manage": "Scan QR and manual check-in",
    "imports.manage": "Import members",
    "plans.manage": "Create and edit plans",
    "staff.manage": "Manage staff and roles",
    "settings.manage": "Gym profile, UPI accounts, branches",
    "activity.view": "See the activity log",
    "reports.export": "Export CSVs",
}
ALL = frozenset(PERMISSIONS)

ROLE_PRESETS = {
    "manager": ALL - {"staff.manage", "settings.manage"},
    "receptionist": frozenset({"members.view", "members.edit", "memberships.manage", "payments.view", "payments.record",
                               "checkins.manage", "imports.manage"}),
    "trainer": frozenset({"members.view", "checkins.manage"}),
    "staff": frozenset({"members.view", "members.edit", "memberships.manage", "checkins.manage", "imports.manage"}),
}
ROLES = ["owner", *ROLE_PRESETS.keys()]
MONEY = {"payments.view", "payments.record"}


def effective_permissions(role: str, staff_role: str | None, can_record_payments: bool,
                          explicit: list | None) -> frozenset:
    if role == "owner":
        return ALL
    if explicit:
        perms = frozenset(p for p in explicit if p in ALL)
    else:
        perms = ROLE_PRESETS.get(staff_role or "staff", ROLE_PRESETS["staff"])
    if can_record_payments:
        perms = perms | MONEY
    return perms
