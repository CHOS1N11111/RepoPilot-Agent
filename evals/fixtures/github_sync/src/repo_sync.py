"""Repository synchronization behavior for remote GitHub branches."""


def sync_remote_branch(remote: str, branch: str, *, clean: bool) -> str:
    """Return the safe GitHub branch sync action for a clean repository."""

    if not clean:
        return "fetch-only"
    return f"fetch {remote}; checkout {branch}; fast-forward"
