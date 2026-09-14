from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from fastapi import HTTPException, Request

from google.auth.transport.requests import (
    AuthorizedSession,
    Request as GoogleRequest,
)
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from database import SessionLocal, User


# ============================================================
# Load environment variables
# ============================================================

load_dotenv()


# ============================================================
# Google OAuth configuration
# ============================================================

GOOGLE_AUTH_BASE_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth"
)

GOOGLE_TOKEN_URL = (
    "https://oauth2.googleapis.com/token"
)

GOOGLE_USERINFO_URL = (
    "https://openidconnect.googleapis.com/v1/userinfo"
)

GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
    (
        "https://www.googleapis.com/auth/"
        "photospicker.mediaitems.readonly"
    ),
]


# ============================================================
# Local development OAuth transport
# ============================================================

def configure_oauth_environment():

    redirect_uri = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "",
    ).strip().lower()

    # --------------------------------------------------------
    # DEVELOPMENT-ONLY HTTPS BYPASS
    #
    # Allows OAuth over:
    # http://localhost
    # http://127.0.0.1
    #
    # MUST NOT be enabled for production.
    # --------------------------------------------------------

    if redirect_uri.startswith(
        (
            "http://localhost",
            "http://127.0.0.1",
        )
    ):

        os.environ[
            "OAUTHLIB_INSECURE_TRANSPORT"
        ] = "1"

    else:

        os.environ.pop(
            "OAUTHLIB_INSECURE_TRANSPORT",
            None,
        )

    # --------------------------------------------------------
    # OAuthLib scope normalization
    #
    # Google may return equivalent normalized OIDC scopes,
    # causing OAuthLib to raise ScopeChangedWarning.
    #
    # This tells OAuthLib to accept the returned scope set.
    # --------------------------------------------------------

    os.environ[
        "OAUTHLIB_RELAX_TOKEN_SCOPE"
    ] = "1"


configure_oauth_environment()


# ============================================================
# Google client configuration
# ============================================================

def get_google_client_config():

    client_id = os.getenv(
        "GOOGLE_CLIENT_ID"
    )

    client_secret = os.getenv(
        "GOOGLE_CLIENT_SECRET"
    )

    redirect_uri = os.getenv(
        "GOOGLE_REDIRECT_URI"
    )

    if not client_id:

        raise RuntimeError(
            "GOOGLE_CLIENT_ID is not configured."
        )

    if not client_secret:

        raise RuntimeError(
            "GOOGLE_CLIENT_SECRET is not configured."
        )

    if not redirect_uri:

        raise RuntimeError(
            "GOOGLE_REDIRECT_URI is not configured."
        )

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }


# ============================================================
# Encryption
# ============================================================

def get_fernet() -> Fernet:

    key = os.getenv(
        "APP_ENCRYPTION_KEY"
    )

    if not key:

        raise RuntimeError(
            "APP_ENCRYPTION_KEY is not configured."
        )

    try:

        return Fernet(
            key.encode("utf-8")
        )

    except Exception as exc:

        raise RuntimeError(
            "APP_ENCRYPTION_KEY is invalid."
        ) from exc


def encrypt_string(
    value: str,
) -> str:

    if not value:

        raise ValueError(
            "Cannot encrypt an empty value."
        )

    encrypted = get_fernet().encrypt(
        value.encode("utf-8")
    )

    return encrypted.decode("utf-8")


def decrypt_string(
    encrypted_value: str,
) -> str:

    if not encrypted_value:

        raise ValueError(
            "Cannot decrypt an empty value."
        )

    try:

        decrypted = get_fernet().decrypt(
            encrypted_value.encode("utf-8")
        )

    except InvalidToken as exc:

        raise RuntimeError(
            "Unable to decrypt stored credential. "
            "The application encryption key may "
            "have changed."
        ) from exc

    return decrypted.decode("utf-8")


# ============================================================
# OAuth state
# ============================================================

def create_oauth_state() -> str:

    return secrets.token_urlsafe(
        32
    )


# ============================================================
# Authorization URL
# ============================================================

def create_authorization_url(
    request: Request,
):

    config = get_google_client_config()

    configure_oauth_environment()

    state = create_oauth_state()

    request.session[
        "google_oauth_state"
    ] = state

    params = {
        "client_id": config[
            "client_id"
        ],
        "redirect_uri": config[
            "redirect_uri"
        ],
        "response_type": "code",
        "scope": " ".join(
            GOOGLE_SCOPES
        ),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }

    return (
        f"{GOOGLE_AUTH_BASE_URL}?"
        f"{urlencode(params)}"
    )


# ============================================================
# Exchange authorization code
# ============================================================

def exchange_code_for_credentials(
    request: Request,
    code: str,
    state: str,
):

    expected_state = request.session.get(
        "google_oauth_state"
    )

    if not expected_state:

        raise HTTPException(
            status_code=400,
            detail=(
                "Google OAuth session is missing "
                "or has expired."
            ),
        )

    if not secrets.compare_digest(
        expected_state,
        state,
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid Google OAuth state."
            ),
        )

    config = get_google_client_config()

    configure_oauth_environment()

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": config[
                    "client_id"
                ],
                "client_secret": config[
                    "client_secret"
                ],
                "auth_uri": GOOGLE_AUTH_BASE_URL,
                "token_uri": GOOGLE_TOKEN_URL,
            }
        },
        scopes=GOOGLE_SCOPES,
        state=state,
        autogenerate_code_verifier=False,
    )

    flow.redirect_uri = config[
        "redirect_uri"
    ]

    authorization_response = str(
        request.url
    )

    try:

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Exchange the authorization code exactly ONCE.
        #
        # The previous implementation retried the same code
        # after a scope warning. That caused:
        #
        # invalid_grant
        #
        # because authorization codes are single-use.
        # ----------------------------------------------------

        flow.fetch_token(
            authorization_response=(
                authorization_response
            )
        )

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail=(
                "Google authorization failed.\n\n"
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    credentials = flow.credentials

    if not credentials.valid:

        raise HTTPException(
            status_code=400,
            detail=(
                "Google returned invalid credentials."
            ),
        )

    # --------------------------------------------------------
    # OAuth state is one-time use.
    # --------------------------------------------------------

    request.session.pop(
        "google_oauth_state",
        None,
    )

    return credentials


# ============================================================
# Credentials → dictionary
# ============================================================

def credentials_to_dict(
    credentials: Credentials,
):

    return {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
        "expiry": (
            credentials.expiry.isoformat()
            if credentials.expiry
            else None
        ),
    }


# ============================================================
# Dictionary → credentials
# ============================================================

def credentials_from_dict(
    token_data: dict,
):

    expiry = token_data.get(
        "expiry"
    )

    expiry_datetime = None

    if expiry:

        try:

            expiry_datetime = (
                datetime.fromisoformat(
                    expiry
                )
            )

        except ValueError:

            expiry_datetime = None

    return Credentials(
        token=token_data.get(
            "token"
        ),
        refresh_token=token_data.get(
            "refresh_token"
        ),
        token_uri=token_data.get(
            "token_uri",
            GOOGLE_TOKEN_URL,
        ),
        client_id=token_data.get(
            "client_id"
        ),
        client_secret=token_data.get(
            "client_secret"
        ),
        scopes=token_data.get(
            "scopes"
        ),
        expiry=expiry_datetime,
    )


# ============================================================
# Encrypt Google credentials
# ============================================================

def encrypt_credentials(
    credentials: Credentials,
) -> str:

    credentials_dict = (
        credentials_to_dict(
            credentials
        )
    )

    token_json = json.dumps(
        credentials_dict
    )

    return encrypt_string(
        token_json
    )


# ============================================================
# Decrypt Google credentials
# ============================================================

def decrypt_credentials(
    encrypted_token: str,
):

    token_json = decrypt_string(
        encrypted_token
    )

    try:

        token_data = json.loads(
            token_json
        )

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "Stored Google credentials "
            "are corrupted."
        ) from exc

    return credentials_from_dict(
        token_data
    )


# ============================================================
# Refresh credentials
# ============================================================

def refresh_credentials(
    credentials: Credentials,
):

    if (
        credentials.expired
        and credentials.refresh_token
    ):

        try:

            credentials.refresh(
                GoogleRequest()
            )

        except Exception as exc:

            raise HTTPException(
                status_code=401,
                detail=(
                    "Google authorization could not "
                    "be refreshed. Please sign in again."
                ),
            ) from exc

    if not credentials.valid:

        raise HTTPException(
            status_code=401,
            detail=(
                "Google authorization is no longer valid. "
                "Please sign in again."
            ),
        )

    return credentials


# ============================================================
# Google account information
# ============================================================

def get_google_user_info(
    credentials: Credentials,
):

    credentials = refresh_credentials(
        credentials
    )

    session = AuthorizedSession(
        credentials
    )

    response = session.get(
        GOOGLE_USERINFO_URL,
        timeout=30,
    )

    if response.status_code != 200:

        raise HTTPException(
            status_code=401,
            detail=(
                "Failed to retrieve Google "
                "account information.\n"
                f"Google response: {response.text}"
            ),
        )

    data = response.json()

    subject_id = data.get(
        "sub"
    )

    email = data.get(
        "email"
    )

    if not subject_id:

        raise HTTPException(
            status_code=401,
            detail=(
                "Google did not return a user ID."
            ),
        )

    if not email:

        raise HTTPException(
            status_code=401,
            detail=(
                "Google did not return an email address."
            ),
        )

    return {
        "subject_id": subject_id,
        "email": email,
        "email_prefix": email.split(
            "@",
            1,
        )[0],
        "name": data.get(
            "name"
        ),
        "picture": data.get(
            "picture"
        ),
    }


# ============================================================
# Get current user's stored Google credentials
# ============================================================

def get_session_credentials(
    request: Request,
):

    user_id = request.session.get(
        "user_id"
    )

    if not user_id:

        raise HTTPException(
            status_code=401,
            detail=(
                "You are not signed in with Google."
            ),
        )

    db = SessionLocal()

    try:

        user = db.get(
            User,
            int(user_id),
        )

        if not user:

            raise HTTPException(
                status_code=401,
                detail=(
                    "The current user account "
                    "could not be found."
                ),
            )

        if not user.encrypted_google_token:

            raise HTTPException(
                status_code=401,
                detail=(
                    "Google authorization is missing. "
                    "Please sign in again."
                ),
            )

        credentials = (
            decrypt_credentials(
                user.encrypted_google_token
            )
        )

        old_token = credentials.token

        credentials = refresh_credentials(
            credentials
        )

        if old_token != credentials.token:

            user.encrypted_google_token = (
                encrypt_credentials(
                    credentials
                )
            )

            db.commit()

        return credentials

    finally:

        db.close()


# ============================================================
# Refresh stored credentials
# ============================================================

def refresh_stored_credentials(
    encrypted_token: str,
):

    credentials = decrypt_credentials(
        encrypted_token
    )

    old_token = credentials.token

    credentials = refresh_credentials(
        credentials
    )

    new_encrypted_token = (
        encrypt_credentials(
            credentials
        )
    )

    token_changed = (
        old_token
        != credentials.token
    )

    return (
        credentials,
        new_encrypted_token,
        token_changed,
    )


# ============================================================
# Clear application session
# ============================================================

def clear_google_session(
    request: Request,
):

    request.session.pop(
        "google_oauth_state",
        None,
    )

    request.session.pop(
        "user_id",
        None,
    )

    request.session.pop(
        "current_album_id",
        None,
    )

    request.session.pop(
        "picker_session_id",
        None,
    )

    request.session.pop(
        "selected_media_count",
        None,
    )