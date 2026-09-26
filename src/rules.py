from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _resolve_pending_correction(actor, entity, resolution):
    correction = (entity.get("data") or {}).get("correction") or {}
    if correction.get("status") != "pending":
        return {}
    resolved = dict(correction)
    resolved["status"] = "resolved"
    resolved["resolved_by"] = actor.user_id
    resolved["resolution"] = resolution
    return {"correction": resolved}


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    extra = {"confirmed_by": actor.user_id}
    extra.update(_resolve_pending_correction(actor, entity, "confirmed"))
    return extra


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")
    return _resolve_pending_correction(actor, entity, "probable")


def _validate_lab_correction(actor, entity, data, lookup):
    correction = {
        "correction_id": data["correction_id"],
        "reason": data["reason"],
        "corrected_result": data["corrected_result"],
        "status": "pending",
        "submitted_by": actor.user_id,
        "previous_confirmation": {
            "result": entity["data"].get("result"),
            "lab_id": entity["data"].get("lab_id"),
            "confirmed_by": entity["data"].get("confirmed_by"),
        },
    }
    return {"correction": correction}


def _validate_complete_followup(actor, entity, data, lookup):
    case = _find_one(lookup, "case", "id", entity["data"].get("case_id"))
    if not case:
        return None
    correction = (case.get("data") or {}).get("correction") or {}
    if correction.get("status") == "pending":
        raise ConflictError(
            "contact %s 卡在 complete_followup：病例 %s 的检验更正 %s 尚未出结论"
            "（病例已回到 investigating，待 lab_positive 或 mark_probable 后才能完成医学观察）"
            % (entity["id"], case["id"], correction.get("correction_id"))
        )
    return None


def cluster_cases(cases, max_days=14):
    groups = []
    for case in sorted(cases, key=lambda item: str(item.get("onset_date", ""))):
        placed = False
        for group in groups:
            same_location = group["location"] == case.get("location")
            delta = abs(_date_ordinal(group["onset_date"]) - _date_ordinal(case.get("onset_date")))
            if same_location and delta <= max_days:
                group["members"].append(case.get("id"))
                placed = True
                break
        if not placed:
            groups.append({"location": case.get("location"), "onset_date": case.get("onset_date"), "members": [case.get("id")]})
    return [group for group in groups if len(group["members"]) > 1]


CUSTOM_CREATE = {'case': _validate_case}
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable, ('case', 'correct_lab_result'): _validate_lab_correction, ('contact', 'complete_followup'): _validate_complete_followup}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'correct_lab_result': (('confirmed',), 'investigating'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'correct_lab_result'): ('correction_id', 'reason', 'corrected_result'), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'correct_lab_result': ('admin', 'lab'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator')}
    IDEMPOTENT_ACTIONS = {('case', 'correct_lab_result'): ('correction', 'correction_id')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def already_applied(self, entity, action, data):
        spec = self.IDEMPOTENT_ACTIONS.get((self.normalize_kind(entity["kind"]), action))
        if not spec:
            return False
        record_field, key_field = spec
        record = (entity.get("data") or {}).get(record_field) or {}
        key = data.get(key_field)
        return bool(key) and record.get(key_field) == key

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
