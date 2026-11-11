"""Request lifecycle state machine. The ONLY place that knows which status
transitions are legal; every status change in the service goes through
``check()``, so an illegal jump (e.g. REJECTED -> APPROVED) is impossible."""

from .errors import InvalidTransition

RECEIVED = "RECEIVED"
INVALID = "INVALID"
IN_PROGRESS = "IN_PROGRESS"
RETRY_PENDING = "RETRY_PENDING"
MANUAL_REVIEW = "MANUAL_REVIEW"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
FAILED = "FAILED"

TRANSITIONS = {
    None: {RECEIVED},
    RECEIVED: {INVALID, IN_PROGRESS},
    IN_PROGRESS: {APPROVED, REJECTED, MANUAL_REVIEW, RETRY_PENDING, FAILED},
    RETRY_PENDING: {IN_PROGRESS},
    MANUAL_REVIEW: {APPROVED, REJECTED},
    APPROVED: set(),
    REJECTED: set(),
    INVALID: set(),
    FAILED: set(),
}

TERMINAL = {APPROVED, REJECTED, INVALID, FAILED}
AWAITING_HUMAN = {MANUAL_REVIEW}


def check(from_status, to_status):
    if to_status not in TRANSITIONS.get(from_status, set()):
        raise InvalidTransition(f"cannot move request from {from_status} to {to_status}",
                                {"from": from_status, "to": to_status,
                                 "allowed": sorted(TRANSITIONS.get(from_status, set()))})
