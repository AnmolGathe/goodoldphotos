import base64
import hashlib
import json
import tarfile
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import database
from database import AndroidOAuthHandoff, AndroidOAuthTransaction, Base, User, utcnow
from backup_engine import (build_asset_name, choose_chunk_items, create_chunk_archive,
                           sanitize_album_name, sanitize_filename)
from download_manager import safe_extract_tar
from app import android_oauth_callback, dashboard, google_android_login, google_callback, google_login


class BackupEngineTests(unittest.TestCase):
    def test_names_are_safe(self):
        self.assertEqual(sanitize_filename('../../bad?.jpg'), 'bad_.jpg')
        self.assertEqual(sanitize_album_name('  My   album '), 'My album')
        self.assertIn('Part 001', build_asset_name('A', 1, 'x.jpg', 'y.jpg'))

    def test_uncompressed_tar_manifest_and_safe_restore(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source = root / 'photo.jpg'; source.write_bytes(b'unchanged bytes')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            info = create_chunk_archive([{'media_id':'local:1:x','filename':'photo.jpg','mime_type':'image/jpeg','size':15,'sha256':digest,'local_path':str(source)}], 'Archive', 1, root)
            with tarfile.open(info['path']) as archive:
                manifest = json.load(archive.extractfile('manifest.json'))
                self.assertEqual(manifest['items'][0]['sha256'], digest)
                self.assertEqual(archive.extractfile('media/000001_photo.jpg').read(), b'unchanged bytes')
            extracted = root / 'restore'; safe_extract_tar(info['path'], extracted)
            self.assertEqual((extracted/'media/000001_photo.jpg').read_bytes(), b'unchanged bytes')

    def test_chunking_preserves_order(self):
        items = [{'filename':str(i), 'size':1, 'local_path':'x'} for i in range(3)]
        self.assertEqual([x['filename'] for x in choose_chunk_items(items)], ['0','1','2'])

    def test_dashboard_allows_anonymous_visitor_to_reach_sign_in_page(self):
        request = SimpleNamespace(session={})
        response = dashboard(request)
        self.assertEqual(response.template.name, 'landing.html')

    def test_oauth_start_normalizes_loopback_host_to_callback_host(self):
        request = SimpleNamespace(url=urlsplit('http://127.0.0.1:8000/auth/google'), session={})
        with patch('app.os.getenv', return_value='http://localhost:8000/auth/google/callback'):
            response = google_login(request)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers['location'], 'http://localhost:8000/auth/google')

    def test_android_oauth_start_creates_session_state_and_persists_its_hash(self):
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(b"android verifier").digest()
        ).rstrip(b"=").decode("ascii")
        state = "existing-oauth-state-value"
        request = SimpleNamespace(session={})
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        database_sessions = sessionmaker(bind=engine)

        try:
            with (
                patch("auth.get_google_client_config", return_value={
                    "client_id": "client-id",
                    "client_secret": "server-only-secret",
                    "redirect_uri": "https://mygoodoldphotos.onrender.com/auth/google/callback",
                }),
                patch("auth.create_oauth_state", return_value=state),
                patch("auth.configure_oauth_environment"),
                patch("app.SessionLocal", database_sessions),
            ):
                response = google_android_login(request, challenge, "S256")

            database_session = database_sessions()
            try:
                transactions = database_session.query(AndroidOAuthTransaction).all()
            finally:
                database_session.close()
        finally:
            engine.dispose()

        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            request.session["google_oauth_state"],
            state,
        )
        query = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertEqual(query["state"], [state])
        self.assertEqual(
            query["redirect_uri"],
            ["https://mygoodoldphotos.onrender.com/auth/google/callback"],
        )
        self.assertNotIn("client_secret", query)
        self.assertEqual(len(transactions), 1)
        transaction = transactions[0]
        self.assertEqual(transaction.state_hash, hashlib.sha256(state.encode()).hexdigest())
        self.assertNotEqual(transaction.state_hash, state)
        self.assertEqual(transaction.code_challenge, challenge)

    def test_android_oauth_transaction_table_is_created_by_existing_schema_init(self):
        engine = create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(engine)
            inspector = inspect(engine)
            transaction_columns = {
                column["name"]
                for column in inspector.get_columns("android_oauth_transactions")
            }
            handoff_columns = {
                column["name"]
                for column in inspector.get_columns("android_oauth_handoffs")
            }
        finally:
            engine.dispose()

        self.assertEqual(
            transaction_columns,
            {"state_hash", "code_challenge", "created_at", "expires_at", "completed_at"},
        )
        self.assertEqual(
            handoff_columns,
            {"code_hash", "user_id", "code_challenge", "expires_at", "redeemed_at"},
        )

    def test_existing_android_transaction_table_gets_expiry_and_completion_columns(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE android_oauth_transactions ("
                "state_hash VARCHAR(64) PRIMARY KEY, "
                "code_challenge VARCHAR(43) NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO android_oauth_transactions "
                "(state_hash, code_challenge, created_at) "
                "VALUES ('state-hash', 'challenge', '2026-10-07 19:00:00')"
            )
            connection.exec_driver_sql(
                "CREATE TABLE android_oauth_handoffs ("
                "code_hash VARCHAR(64) PRIMARY KEY, "
                "user_id INTEGER NOT NULL, "
                "expires_at DATETIME NOT NULL, "
                "redeemed_at DATETIME)"
            )
            connection.exec_driver_sql(
                "INSERT INTO android_oauth_handoffs "
                "(code_hash, user_id, expires_at) "
                "VALUES ('code-hash', 1, '2026-10-07 19:10:00')"
            )

        try:
            with (
                patch("database.engine", engine),
                patch("database.DB_URL", "sqlite:///:memory:"),
            ):
                database._sqlite_add_missing_columns()
            db = engine.connect()
            try:
                columns = {
                    column["name"]
                    for column in inspect(engine).get_columns(
                        "android_oauth_transactions"
                    )
                }
                expiry = db.exec_driver_sql(
                    "SELECT expires_at FROM android_oauth_transactions "
                    "WHERE state_hash = 'state-hash'"
                ).scalar_one()
                challenge = db.exec_driver_sql(
                    "SELECT code_challenge FROM android_oauth_handoffs "
                    "WHERE code_hash = 'code-hash'"
                ).scalar_one()
                handoff_columns = {
                    column["name"]
                    for column in inspect(engine).get_columns(
                        "android_oauth_handoffs"
                    )
                }
            finally:
                db.close()
        finally:
            engine.dispose()

        self.assertIn("expires_at", columns)
        self.assertIn("completed_at", columns)
        self.assertEqual(expiry, "2026-10-07 19:10:00")
        self.assertIn("code_challenge", handoff_columns)
        self.assertEqual(challenge, "")

    def test_android_oauth_start_rejects_missing_challenge(self):
        with self.assertRaises(HTTPException) as raised:
            google_android_login(SimpleNamespace(session={}), None, "S256")
        self.assertEqual(raised.exception.status_code, 400)

    def test_android_oauth_start_rejects_invalid_challenge(self):
        with self.assertRaises(HTTPException) as raised:
            google_android_login(SimpleNamespace(session={}), "A" * 42 + "!", "S256")
        self.assertEqual(raised.exception.status_code, 400)

    def test_android_oauth_start_rejects_non_s256_method(self):
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(b"android verifier").digest()
        ).rstrip(b"=").decode("ascii")
        with self.assertRaises(HTTPException) as raised:
            google_android_login(SimpleNamespace(session={}), challenge, "plain")
        self.assertEqual(raised.exception.status_code, 400)

    def test_normal_hosted_oauth_start_still_uses_existing_flow(self):
        request = SimpleNamespace(
            url=urlsplit("https://mygoodoldphotos.onrender.com/auth/google"),
            session={},
        )
        authorization_url = "https://accounts.google.com/o/oauth2/v2/auth?existing=1"
        with (
            patch(
                "app.os.getenv",
                return_value="https://mygoodoldphotos.onrender.com/auth/google/callback",
            ),
            patch("app.create_authorization_url", return_value=authorization_url) as create_url,
        ):
            response = google_login(request)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], authorization_url)
        create_url.assert_called_once_with(request)

    def _callback_database(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        return engine, sessionmaker(bind=engine)

    def _run_callback_with_patches(self, request, sessions):
        oauth_state = request.session["google_oauth_state"]

        def validate_existing_state(callback_request, code, state):
            self.assertEqual(code, "google-authorization-code")
            self.assertEqual(state, oauth_state)
            self.assertEqual(callback_request.session["google_oauth_state"], state)
            callback_request.session.pop("google_oauth_state")
            return object()

        with (
            patch("app.exchange_code_for_credentials", side_effect=validate_existing_state),
            patch("app.get_google_user_info", return_value={
                "subject_id": "google-subject-1",
                "email": "person@example.com",
                "name": "Example Person",
            }),
            patch("app.encrypt_credentials", return_value="encrypted-google-token"),
            patch("app.SessionLocal", sessions),
        ):
            return google_callback(
                request,
                "google-authorization-code",
                oauth_state,
            )

    def test_normal_desktop_oauth_callback_still_upserts_user_and_redirects_home(self):
        oauth_state = "desktop-state"
        request = SimpleNamespace(session={"google_oauth_state": oauth_state})
        engine, sessions = self._callback_database()
        try:
            response = self._run_callback_with_patches(request, sessions)
            db = sessions()
            try:
                user = db.query(User).filter_by(google_subject_id="google-subject-1").one()
            finally:
                db.close()
        finally:
            engine.dispose()

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        self.assertEqual(user.email, "person@example.com")
        self.assertEqual(user.encrypted_google_token, "encrypted-google-token")
        self.assertEqual(request.session, {"user_id": user.id})

    def test_android_callback_creates_hashed_five_minute_handoff(self):
        oauth_state = "android-state"
        state_hash = hashlib.sha256(oauth_state.encode()).hexdigest()
        request = SimpleNamespace(session={
            "google_oauth_state": oauth_state,
            "android_oauth_state_hash": state_hash,
        })
        engine, sessions = self._callback_database()
        db = sessions()
        db.add(
            AndroidOAuthTransaction(
                state_hash=state_hash,
                code_challenge="A" * 43,
                expires_at=utcnow() + timedelta(minutes=10),
            )
        )
        db.commit()
        db.close()

        try:
            before = utcnow()
            response = self._run_callback_with_patches(request, sessions)
            after = utcnow()
            db = sessions()
            try:
                handoff = db.query(AndroidOAuthHandoff).one()
                user = db.query(User).filter_by(google_subject_id="google-subject-1").one()
                transaction = db.get(AndroidOAuthTransaction, state_hash)
            finally:
                db.close()
        finally:
            engine.dispose()

        self.assertEqual(response.status_code, 303)
        location = urlsplit(response.headers["location"])
        self.assertEqual(location.scheme, "https")
        self.assertEqual(location.netloc, "mygoodoldphotos.onrender.com")
        self.assertEqual(location.path, "/android/oauth/callback")
        params = parse_qs(location.query)
        self.assertEqual(set(params), {"code"})
        raw_code = params["code"][0]
        self.assertEqual(len(raw_code), 43)
        self.assertEqual(
            handoff.code_hash,
            hashlib.sha256(raw_code.encode("ascii")).hexdigest(),
        )
        self.assertNotEqual(handoff.code_hash, raw_code)
        self.assertEqual(handoff.user_id, user.id)
        self.assertIsNone(handoff.redeemed_at)
        expires_at = handoff.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=before.tzinfo)
        self.assertGreaterEqual(expires_at, before + timedelta(minutes=5))
        self.assertLessEqual(expires_at, after + timedelta(minutes=5))
        self.assertIsNotNone(transaction.completed_at)
        self.assertEqual(
            request.session,
            {"user_id": user.id},
        )

    def test_android_callback_rejects_missing_transaction(self):
        oauth_state = "missing-android-transaction"
        state_hash = hashlib.sha256(oauth_state.encode()).hexdigest()
        request = SimpleNamespace(session={
            "google_oauth_state": oauth_state,
            "android_oauth_state_hash": state_hash,
        })
        engine, sessions = self._callback_database()
        try:
            with (
                patch("app.exchange_code_for_credentials") as exchange,
                patch("app.get_google_user_info", return_value={
                    "subject_id": "google-subject-1",
                    "email": "person@example.com",
                    "name": "Example Person",
                }),
                patch("app.SessionLocal", sessions),
            ):
                exchange.return_value = object()
                with self.assertRaises(HTTPException) as raised:
                    google_callback(request, "code", oauth_state)
        finally:
            engine.dispose()

        self.assertEqual(raised.exception.status_code, 400)

    def test_android_callback_rejects_expired_transaction(self):
        oauth_state = "expired-android-transaction"
        state_hash = hashlib.sha256(oauth_state.encode()).hexdigest()
        request = SimpleNamespace(session={
            "google_oauth_state": oauth_state,
            "android_oauth_state_hash": state_hash,
        })
        engine, sessions = self._callback_database()
        db = sessions()
        db.add(
            AndroidOAuthTransaction(
                state_hash=state_hash,
                code_challenge="A" * 43,
                expires_at=utcnow() - timedelta(seconds=1),
            )
        )
        db.commit()
        db.close()

        try:
            with (
                patch("app.exchange_code_for_credentials", return_value=object()),
                patch("app.get_google_user_info", return_value={
                    "subject_id": "google-subject-1",
                    "email": "person@example.com",
                    "name": "Example Person",
                }),
                patch("app.SessionLocal", sessions),
            ):
                with self.assertRaises(HTTPException) as raised:
                    google_callback(request, "code", oauth_state)
            db = sessions()
            try:
                transaction = db.get(AndroidOAuthTransaction, state_hash)
                handoff_count = db.query(AndroidOAuthHandoff).count()
            finally:
                db.close()
        finally:
            engine.dispose()

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIsNone(transaction.completed_at)
        self.assertEqual(handoff_count, 0)

    def test_android_transaction_cannot_create_two_handoffs(self):
        oauth_state = "single-use-android-transaction"
        state_hash = hashlib.sha256(oauth_state.encode()).hexdigest()
        engine, sessions = self._callback_database()
        db = sessions()
        db.add(
            AndroidOAuthTransaction(
                state_hash=state_hash,
                code_challenge="A" * 43,
                expires_at=utcnow() + timedelta(minutes=10),
            )
        )
        db.commit()
        db.close()

        try:
            first_request = SimpleNamespace(session={
                "google_oauth_state": oauth_state,
                "android_oauth_state_hash": state_hash,
            })
            first_response = self._run_callback_with_patches(first_request, sessions)
            second_request = SimpleNamespace(session={
                "google_oauth_state": oauth_state,
                "android_oauth_state_hash": state_hash,
            })
            with (
                patch("app.exchange_code_for_credentials", return_value=object()),
                patch("app.get_google_user_info", return_value={
                    "subject_id": "google-subject-1",
                    "email": "person@example.com",
                    "name": "Example Person",
                }),
                patch("app.encrypt_credentials", return_value="encrypted-google-token"),
                patch("app.SessionLocal", sessions),
            ):
                with self.assertRaises(HTTPException) as raised:
                    google_callback(second_request, "code", oauth_state)
            db = sessions()
            try:
                handoffs = db.query(AndroidOAuthHandoff).count()
            finally:
                db.close()
        finally:
            engine.dispose()

        self.assertEqual(first_response.status_code, 303)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(handoffs, 1)

    def test_android_handoff_landing_response_does_not_echo_code(self):
        response = android_oauth_callback()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Android authentication handoff received.", response.body.decode())
        self.assertNotIn("code", response.body.decode())


if __name__ == '__main__': unittest.main()
