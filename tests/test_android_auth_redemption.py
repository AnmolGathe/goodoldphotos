from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import (
    AndroidHandoffRedemption,
    redeem_android_handoff,
    redeem_android_handoff_locally,
)
from database import AndroidOAuthHandoff, Base, User, utcnow


def verifier_for(seed: bytes) -> str:
    return base64.urlsafe_b64encode(seed).rstrip(b"=").decode("ascii")


def challenge_for(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")


class AndroidAuthRedemptionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{Path(self.temp_dir.name) / 'redemption.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.session_patcher = patch("app.SessionLocal", self.sessions)
        self.session_patcher.start()
        self.code = verifier_for(b"h" * 32)
        self.verifier = verifier_for(b"v" * 32)
        self.session = self.sessions()
        user = User(
            google_subject_id="google-subject",
            email="android@example.com",
            email_prefix="android",
            display_name="Android User",
            storage_mode="platform",
        )
        self.session.add(user)
        self.session.flush()
        self.user_id = user.id
        self.session.add(
            AndroidOAuthHandoff(
                code_hash=hashlib.sha256(self.code.encode("ascii")).hexdigest(),
                user_id=user.id,
                code_challenge=challenge_for(self.verifier),
                expires_at=utcnow() + timedelta(minutes=5),
            )
        )
        self.session.commit()
        self.session.close()

    def tearDown(self):
        self.session_patcher.stop()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _handoff(self):
        session = self.sessions()
        try:
            return session.query(AndroidOAuthHandoff).one()
        finally:
            session.close()

    def test_render_redemption_returns_only_minimum_user_identity_and_consumes_code(self):
        response = redeem_android_handoff(
            AndroidHandoffRedemption(code=self.code, code_verifier=self.verifier)
        )

        self.assertEqual(
            response,
            {
                "user": {
                    "google_subject_id": "google-subject",
                    "email": "android@example.com",
                    "display_name": "Android User",
                }
            },
        )
        self.assertIsNotNone(self._handoff().redeemed_at)

    def test_render_redemption_rejects_wrong_verifier_without_consuming_code(self):
        with self.assertRaises(HTTPException) as raised:
            redeem_android_handoff(
                AndroidHandoffRedemption(
                    code=self.code,
                    code_verifier=verifier_for(b"x" * 32),
                )
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIsNone(self._handoff().redeemed_at)

    def test_render_redemption_rejects_invalid_code(self):
        with self.assertRaises(HTTPException) as raised:
            redeem_android_handoff(
                AndroidHandoffRedemption(
                    code=verifier_for(b"x" * 32),
                    code_verifier=self.verifier,
                )
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_render_redemption_rejects_expired_handoff(self):
        session = self.sessions()
        session.query(AndroidOAuthHandoff).one().expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
        session.close()

        with self.assertRaises(HTTPException) as raised:
            redeem_android_handoff(
                AndroidHandoffRedemption(code=self.code, code_verifier=self.verifier)
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_render_redemption_rejects_replay(self):
        payload = AndroidHandoffRedemption(code=self.code, code_verifier=self.verifier)
        redeem_android_handoff(payload)

        with self.assertRaises(HTTPException) as raised:
            redeem_android_handoff(payload)
        self.assertEqual(raised.exception.status_code, 400)

    def test_local_redemption_creates_local_user_and_session_cookie(self):
        render_user = {
            "google_subject_id": "render-google-subject",
            "email": "local@example.com",
            "display_name": "Local User",
            "id": 999999,
        }
        render_response = Mock(status_code=200)
        render_response.json.return_value = {"user": render_user}
        request = type("Request", (), {"session": {"preexisting": "value"}})()
        with (
            patch("app.requests.post", return_value=render_response) as render_post,
            patch("app.SessionLocal", self.sessions),
        ):
            response = redeem_android_handoff_locally(
                request,
                AndroidHandoffRedemption(
                    code=self.code,
                    code_verifier=self.verifier,
                ),
            )

        render_post.assert_called_once()
        self.assertEqual(
            render_post.call_args.args[0],
            "https://mygoodoldphotos.onrender.com/auth/google/android/redeem",
        )
        self.assertEqual(render_post.call_args.kwargs["timeout"], (5, 15))
        self.assertFalse(render_post.call_args.kwargs["allow_redirects"])
        self.assertEqual(response, {"status": "ok"})

        session = self.sessions()
        try:
            local_user = session.query(User).filter_by(
                google_subject_id="render-google-subject"
            ).one()
            self.assertNotEqual(local_user.id, render_user["id"])
            self.assertEqual(session.get(User, render_user["id"]), None)
            self.assertEqual(request.session, {"user_id": local_user.id})
        finally:
            session.close()

    def test_local_redemption_render_failure_does_not_create_session(self):
        render_response = Mock(status_code=400)
        request = type("Request", (), {"session": {}})()
        with (
            patch("app.requests.post", return_value=render_response),
            patch("app.SessionLocal", self.sessions),
        ):
            with self.assertRaises(HTTPException) as raised:
                redeem_android_handoff_locally(
                    request,
                    AndroidHandoffRedemption(
                        code=self.code,
                        code_verifier=self.verifier,
                    ),
                )

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(request.session, {})
        session = self.sessions()
        try:
            self.assertEqual(
                session.query(User).filter_by(
                    google_subject_id="render-google-subject"
                ).count(),
                0,
            )
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
