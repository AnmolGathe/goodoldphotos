# MyGoodOldPhotos

A private, byte-preserving backup service for selected Google Photos media and local files. It writes **uncompressed** TAR assets to private GitHub Releases; media is never resized, transcoded, or compressed by this application. Google Photos content is never deleted.

## Start

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app:app --reload
# separate terminal
.venv/bin/python worker.py
```

`database.py` runs an additive SQLite compatibility migration at startup, so existing local development data is retained. For PostgreSQL set `DATABASE_URL=postgresql+psycopg://...`; the worker uses `FOR UPDATE SKIP LOCKED` there.

## Configuration

Create `APP_ENCRYPTION_KEY` with `.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`, and set a random `APP_SECRET_KEY`. Configure a Google OAuth web client with exactly the redirect URI in `GOOGLE_REDIRECT_URI`, enable Google Photos Picker API, and request the Picker readonly scope. HTTP OAuth is permitted only for `localhost` or `127.0.0.1`; production needs HTTPS and `COOKIE_SECURE=true`.

Platform storage requires `PLATFORM_GITHUB_TOKEN`. For a fine-grained personal token, grant repository administration/content access sufficient to create or access a private repository plus **Contents: read/write** and **Metadata: read**; Releases/assets require the repository’s contents permission. If repository creation is disallowed, create the private repository first and store its mapping through the storage configuration workflow.

## Test

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall .
```

The worker is the only process that downloads Picker bytes, creates TARs, uploads assets, or removes verified temporary files. A worker restart requeues stale claims safely.
