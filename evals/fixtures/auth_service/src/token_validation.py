"""Authentication token validation and expired-token rejection helpers."""


def validate_token(token: dict[str, object]) -> bool:
    """Reject expired authentication tokens and accept active credentials."""

    return bool(token.get("active")) and not bool(token.get("expired"))


def login_user(username: str, token: dict[str, object]) -> bool:
    """Authenticate a named user with a valid token."""

    return bool(username.strip()) and validate_token(token)
