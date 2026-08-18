"""Authentication token validation helpers."""


def validate_token(token: dict[str, object]) -> bool:
    """Return whether the token can authenticate a user."""

    return bool(token.get("active"))


def login_user(username: str, token: dict[str, object]) -> bool:
    """Authenticate a named user with a valid token."""

    return bool(username.strip()) and validate_token(token)
