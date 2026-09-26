from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
    WorkflowBlocked,
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


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    patch = {"confirmed_by": actor.user_id}
    patch.update(_resolution_patch(entity, "lab_positive"))
    return patch


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")
    return _resolution_patch(entity, "mark_probable")


def _validate_correct_lab_result(actor, entity, data, lookup):
    # The original confirmation must survive the correction, so snapshot the
    # lab fields that the new conclusion is about to overwrite.
    prior = {
        key: entity["data"].get(key)
        for key in ("lab_id", "result", "confirmed_by")
        if key in entity["data"]
    }
    record = {
        "correction_id": data["correction_id"],
        "reason": data["reason"],
        "new_conclusion": data["new_conclusion"],
        "lab_id": data["lab_id"],
        "corrected_by": actor.user_id,
        "corrected_at": _utcnow(),
        "prior": prior,
        "status": "pending",
    }
    corrections = list(entity["data"].get("corrections") or [])
    corrections.append(record)
    return {"corrections": corrections}


def _validate_complete_followup(actor, entity, data, lookup):
    case = _find_one(lookup, "case", "id", entity["data"].get("case_id"))
    pending = _pending_correction(case["data"]) if case else None
    if pending:
        raise WorkflowBlocked(
            "contact follow-up is blocked at step 'complete_followup': "
            "linked case %s is back in investigation awaiting a revised "
            "conclusion (correction %s)"
            % (case["id"], pending.get("correction_id"))
        )


def _pending_correction(data):
    for item in (data or {}).get("corrections") or []:
        if item.get("status") == "pending":
            return item
    return None


def _resolution_patch(entity, action):
    # Re-issuing a conclusion while a correction is pending resolves it; the
    # same case record keeps being used.
    pending = _pending_correction(entity["data"])
    if not pending:
        return {}
    corrections = []
    for item in entity["data"].get("corrections") or []:
        if item.get("status") == "pending":
            resolved = dict(item)
            resolved["status"] = "resolved"
            resolved["resolved_at"] = _utcnow()
            resolved["resolved_via"] = action
            corrections.append(resolved)
        else:
            corrections.append(item)
    return {"corrections": corrections}


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
CUSTOM_TRANSITIONS = {
    ('case', 'lab_positive'): _validate_lab_positive,
    ('case', 'mark_probable'): _validate_probable,
    ('case', 'correct_lab_result'): _validate_correct_lab_result,
    ('contact', 'complete_followup'): _validate_complete_followup,
}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {
        'case': {
            'triage': (('reported',), 'investigating'),
            'lab_positive': (('investigating',), 'confirmed'),
            'mark_probable': (('investigating',), 'probable'),
            # Error-correction path: after a lab result is entered wrongly the
            # case is usually already confirmed. The case goes back to
            # investigation to await the revised conclusion; the audit trail
            # and contact links are kept intact.
            'correct_lab_result': (('confirmed',), 'investigating'),
            'recover': (('confirmed', 'probable'), 'recovered'),
            'close': (('recovered',), 'closed'),
        },
        'contact': {
            'begin_followup': (('identified',), 'following'),
            'complete_followup': (('following',), 'completed'),
        },
    }
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start')}
    ACTION_REQUIRED = {
        ('case', 'triage'): ('clinician',),
        ('case', 'lab_positive'): ('lab_id', 'result'),
        ('case', 'mark_probable'): ('epi_link',),
        ('case', 'correct_lab_result'): ('correction_id', 'reason', 'new_conclusion', 'lab_id'),
        ('case', 'recover'): ('recovered_at',),
        ('case', 'close'): ('outcome',),
        ('contact', 'begin_followup'): ('followup_start', 'due_at'),
        ('contact', 'complete_followup'): ('outcome',),
    }
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {
        'triage': ('admin', 'clinician'),
        'lab_positive': ('admin', 'lab'),
        'mark_probable': ('admin', 'investigator'),
        'correct_lab_result': ('admin', 'lab'),
        'recover': ('admin', 'clinician'),
        'close': ('admin', 'investigator'),
        'begin_followup': ('admin', 'investigator'),
        'complete_followup': ('admin', 'investigator'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def find_correction(entity, correction_id):
        """Return the recorded correction with this id, if the same correction
        was already applied to the case."""
        for item in (entity.get("data") or {}).get("corrections") or []:
            if item.get("correction_id") == correction_id:
                return item
        return None

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
