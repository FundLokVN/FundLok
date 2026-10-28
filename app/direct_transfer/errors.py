"""Errors for the direct-transfer module (spec: docs/specs/direct_transfer/funding-window.md section 4).

Service functions raise `DirectTransferError(status_code, detail)`; Phat's routers map it to
`{"detail": "<message>"}`. `detail` is always a short, user-safe string -- never an internal
exception message (CLAUDE.md: never expose internal exceptions).
"""


class DirectTransferError(Exception):
    def __init__(self, status_code: int, detail: str, code: str | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        # Machine-readable code (e.g. CAPACITY_EXCEEDED) for the frontend; `detail` is the message.
        self.code = code or detail
        # True when the router must COMMIT the session before responding, because the service wrote an audit row
        # (e.g. DUPLICATE_REJECTED in the evidence log) that must survive the error response.
        self.persist = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DirectTransferError({self.status_code}, {self.code!r}, {self.detail!r})"


def conflict(code: str, detail: str | None = None) -> DirectTransferError:
    return DirectTransferError(409, detail or code, code)


def not_found(detail: str = "Not found") -> DirectTransferError:
    return DirectTransferError(404, detail, "NOT_FOUND")


def bad_request(detail: str, code: str | None = None) -> DirectTransferError:
    return DirectTransferError(400, detail, code)
