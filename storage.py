from __future__ import annotations

import os
import re

from dotenv import load_dotenv

from auth import decrypt_string
from database import User
from github import GitHubClient


# ============================================================
# Load environment variables
# ============================================================

load_dotenv()


# ============================================================
# Configuration
# ============================================================

PLATFORM_GITHUB_TOKEN = os.getenv(
    "PLATFORM_GITHUB_TOKEN"
)


# ============================================================
# Repository name
# ============================================================

def sanitize_repository_name(
    value: str,
) -> str:
    """
    Convert the Google email prefix into a valid GitHub
    repository name.
    """

    value = value.strip()

    value = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        value,
    )

    value = value.strip(
        ".-"
    )

    if not value:
        raise ValueError(
            "Could not create a valid GitHub repository "
            "name from the user's email prefix."
        )

    return value


# ============================================================
# Platform repository name collision handling
# ============================================================

def find_platform_repository_name(
    github: GitHubClient,
    email_prefix: str,
) -> str:
    """
    Generate the user's repository name.

    Preferred:
        john

    If already occupied:
        john-1
        john-2
        ...
    """

    base_name = (
        sanitize_repository_name(
            email_prefix
        )
    )

    owner = (
        github.get_authenticated_username()
    )

    candidate = base_name

    counter = 1

    while github.repository_exists(
        owner,
        candidate,
    ):

        candidate = (
            f"{base_name}-{counter}"
        )

        counter += 1

    return candidate


# ============================================================
# Get GitHub client for a user
# ============================================================

def get_user_github_client(
    user: User,
) -> GitHubClient:
    """
    Return the correct GitHub client for the user's
    selected storage mode.

    Does not create repositories.
    """

    # --------------------------------------------------------
    # Platform storage
    # --------------------------------------------------------

    if user.storage_mode == "platform":

        if not PLATFORM_GITHUB_TOKEN:

            raise RuntimeError(
                "PLATFORM_GITHUB_TOKEN is not configured."
            )

        github = GitHubClient(
            PLATFORM_GITHUB_TOKEN
        )

    # --------------------------------------------------------
    # Personal GitHub storage
    # --------------------------------------------------------

    elif user.storage_mode == "personal":

        if not user.encrypted_github_token:

            raise RuntimeError(
                "Your GitHub PAT is not configured. "
                "Open Settings and configure GitHub storage."
            )

        github_token = decrypt_string(
            user.encrypted_github_token
        )

        github = GitHubClient(
            github_token
        )

    # --------------------------------------------------------
    # Invalid mode
    # --------------------------------------------------------

    else:

        raise RuntimeError(
            f"Unknown storage mode: "
            f"{user.storage_mode}"
        )

    return github


# ============================================================
# Ensure user's repository exists
# ============================================================

def ensure_user_repository(
    user: User,
):
    """
    Make sure the user's private backup repository exists.

    Returns:
        (GitHubClient, repository)
    """

    github = get_user_github_client(
        user
    )

    # --------------------------------------------------------
    # Existing repository already stored in DB
    # --------------------------------------------------------

    if (
        user.github_username
        and user.github_repo_name
    ):

        repository = (
            github.get_repository(
                user.github_username,
                user.github_repo_name,
            )
        )

        if not repository.get(
            "private"
        ):

            raise RuntimeError(
                "Your configured backup repository "
                "is not private."
            )

        github.set_repository(
            user.github_username,
            user.github_repo_name,
        )

        return github, repository

    # --------------------------------------------------------
    # Determine authenticated GitHub account
    # --------------------------------------------------------

    github_username = (
        github.get_authenticated_username()
    )

    # --------------------------------------------------------
    # Generate repository name
    # --------------------------------------------------------

    if user.storage_mode == "platform":

        repo_name = (
            find_platform_repository_name(
                github,
                user.email_prefix,
            )
        )

    else:

        repo_name = (
            sanitize_repository_name(
                user.email_prefix
            )
        )

    # --------------------------------------------------------
    # Automatically create private repository
    # --------------------------------------------------------

    repository = (
        github.ensure_private_repository(
            repo_name
        )
    )

    # --------------------------------------------------------
    # Persist repository mapping
    #
    # We deliberately open a new DB session rather than
    # modifying the detached SQLAlchemy object supplied by
    # the caller.
    # --------------------------------------------------------

    from database import SessionLocal

    db = SessionLocal()

    try:

        db_user = db.get(
            User,
            user.id,
        )

        if not db_user:

            raise RuntimeError(
                "User account no longer exists."
            )

        db_user.github_username = (
            repository[
                "owner"
            ][
                "login"
            ]
        )

        db_user.github_repo_name = (
            repository[
                "name"
            ]
        )

        db_user.github_repo_id = (
            repository[
                "id"
            ]
        )

        db.commit()

        db.refresh(
            db_user
        )

        # ----------------------------------------------------
        # Keep the client aligned with the stored repo.
        # ----------------------------------------------------

        github.set_repository(
            db_user.github_username,
            db_user.github_repo_name,
        )

        return github, repository

    finally:

        db.close()


# ============================================================
# Get already-configured storage
# ============================================================

def get_configured_storage(
    user: User,
) -> GitHubClient:
    """
    Return a GitHub client using the repository already
    assigned to the user.

    Raises an error when repository setup has not happened.
    """

    github = get_user_github_client(
        user
    )

    if (
        not user.github_username
        or not user.github_repo_name
    ):

        raise RuntimeError(
            "GitHub storage repository is not configured. "
            "Open Settings and configure storage first."
        )

    github.set_repository(
        user.github_username,
        user.github_repo_name,
    )

    return github