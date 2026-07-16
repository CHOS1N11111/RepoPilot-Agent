"""Validation response helpers for the dashboard fixture."""


def validation_payload(message: str) -> dict[str, str]:
    return {"status": "error", "message": message}
