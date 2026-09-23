# MyGoodOldPhotos

MyGoodOldPhotos is a personal photo/video backup application that backs up user-selected Google Photos media and local files to private GitHub repositories using GitHub Releases.

The application's goal is simple:

> Back up your photos and videos to a private GitHub repository without keeping a permanent copy of the actual media on the application server.

## What the application does

### Google Photos backup

1. Sign in with Google.
2. Open the Google Photos Picker.
3. Select photos/videos.
4. Create a new album or select an existing MyGoodOldPhotos album.
5. Queue the backup.
6. The backup worker retrieves the selected media.
7. Media is packaged into TAR chunks.
8. Each chunk contains:
   - `manifest.json`
   - `media/`
9. Chunks are uploaded as GitHub Release assets.
10. SHA-256 is calculated and verified against the GitHub asset digest.
11. Temporary processing files are deleted after successful verification.
12. The Google Photos Picker session is cleaned up after the backup completes.

### Local file backup

Local photos/videos can also be selected from the user's device.

The application temporarily receives and processes the selected files, packages them into TAR chunks, uploads those chunks to GitHub Releases, verifies the uploaded data, and removes the temporary server-side copies after successful backup.

The application is not intended to keep permanent copies of the user's original local files.

## GitHub storage

Each user's backup is isolated in a private GitHub repository.

There are two storage modes:

### Platform storage

MyGoodOldPhotos uses platform-managed GitHub storage.

The user does not need to provide a GitHub PAT. A private repository is created automatically for the user's backups.

### Personal storage

The user can provide their own GitHub PAT.

A private repository is created or used in the user's GitHub account.

## Backup organization

GitHub Releases are used as the backup storage layer.

Conceptually:

```text
Private GitHub repository
│
└── Releases
    ├── photos-backup-0001
    │   ├── Album - Part 001 - first to last.tar
    │   ├── Album - Part 002 - first to last.tar
    │   └── ...
    │
    ├── photos-backup-0002
    │   └── ...
    │
    └── ...
```

A release can contain up to 1000 backup assets.

When the current release reaches the asset limit, a new release is created.

The target TAR chunk size is approximately 1.8 GB (decimal).

## Data and security model

The application separates application metadata from the actual backup data.

Application metadata includes information such as:

- user/account information
- albums
- backup jobs
- backup progress and status
- media metadata
- GitHub repository mappings

The actual photo/video backup data is stored in the user's private GitHub repository as GitHub Release assets.

### Credentials

Secrets are not stored in the source repository.

Local development uses:

```text
.env
```

GitHub PATs are encrypted before being stored by the application.

Browser sessions do not contain raw Google or GitHub credentials.

### Temporary files

Files used during backup processing are temporary.

After a successful GitHub upload and digest verification, the corresponding temporary data is removed from the application server.

## Project structure

Important files:

```text
app.py                  Main FastAPI application
auth.py                 Google OAuth/session/encryption helpers
database.py             SQLAlchemy models and database setup
google_photos.py        Google Photos Picker/client integration
github.py               GitHub API client
backup_engine.py        Backup/chunk/archive/verification logic
worker.py               Background backup worker
worker_notify.py        Job notification mechanism
render_start.py         Production process launcher
album_manager.py        Album-related helpers
album_routes.py         Album web routes
storage.py              Storage-related helpers
download.py             Backup downloader
download_manager.py     Download/restore helpers
delete.py               Deletion helpers
templates/              Web UI templates
static/                 Static assets
tests/                  Automated tests
requirements.txt        Python dependencies
.gitignore              Local/secrets/generated-file exclusions
```

## Requirements

For local development:

- Python 3.11+ recommended
- Git
- Google Cloud project with Google Photos Picker API enabled
- Google OAuth client
- GitHub account
- GitHub PAT when testing personal GitHub storage

## Local development

### 1. Clone the repository

```bash
git clone <your-repository-url>
cd goodoldphotos
```

### 2. Create a virtual environment

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create `.env`

Create:

```text
.env
```

Example:

```env
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
GOOGLE_REDIRECT_URI=http://localhost:8000/auth/google/callback

APP_SECRET_KEY=your-random-secret
APP_ENCRYPTION_KEY=your-fernet-key

# Only required when platform storage is used:
PLATFORM_GITHUB_TOKEN=your-platform-github-token
```

For local development, the application uses SQLite unless a `DATABASE_URL` is configured.

Do not commit `.env`.

### 5. Configure Google OAuth

Use this callback for local development:

```text
http://localhost:8000/auth/google/callback
```

The same callback must be registered in the Google OAuth client.

### 6. Start the web application

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

### 7. Start the worker

Open another terminal:

```bash
cd goodoldphotos
source .venv/bin/activate
python worker.py
```

The local worker waits for backup-job notifications and processes queued jobs.

### 8. Run tests

```bash
pytest
```

Python syntax checks:

```bash
python -m py_compile \
    app.py \
    auth.py \
    backup_engine.py \
    database.py \
    github.py \
    google_photos.py \
    worker.py \
    worker_notify.py \
    render_start.py
```

## Application flow

### Google Photos

```text
Sign in with Google
        │
        ▼
Google Photos Picker
        │
        ▼
Select photos/videos
        │
        ▼
Choose/create album
        │
        ▼
Queue backup
        │
        ▼
Background worker
        │
        ├── Retrieve selected media
        ├── Create metadata
        ├── Build TAR chunks
        ├── Create/get GitHub release
        ├── Upload chunk
        ├── Verify SHA-256
        └── Remove temporary files
        │
        ▼
Completed
```

### Local files

```text
Select local files
        │
        ▼
Temporary upload/processing
        │
        ▼
Queue backup
        │
        ▼
Background worker
        │
        ├── Build TAR chunks
        ├── Upload to GitHub release
        ├── Verify SHA-256
        └── Delete temporary originals
        │
        ▼
Completed
```

## Restore

Backups can be downloaded from GitHub Releases and restored using the application's restore/download functionality.

TAR manifests and release metadata are used to reconstruct the backed-up media.

## Deletion behavior

Deleting backup data from MyGoodOldPhotos does not delete anything from Google Photos.

Account and album deletion operations are designed to remove the application's associated backup data while leaving the user's Google Photos library untouched.

## Repository hygiene

The repository must never contain secrets or local runtime data.

The project's `.gitignore` excludes files such as:

```text
.env
credentials.json
token.json
*.db
*.sqlite
*.sqlite3
backup_state.json
artifacts/
downloads/
*.tar
*.tar.gz
*.zip
*.part
.venv/
__pycache__/
.pytest_cache/
.vscode/
.idea/
```

Before committing:

```bash
git status
```

Check ignored local files:

```bash
git check-ignore -v \
.env \
credentials.json \
token.json \
mygoodoldphotos.db \
backup_state.json \
downloads \
artifacts
```

Check whether sensitive files are already tracked:

```bash
git ls-files \
.env \
credentials.json \
token.json \
mygoodoldphotos.db \
backup_state.json
```

The last command should return no output.

## Troubleshooting

### Google OAuth redirect error

Verify that the callback URL configured in Google OAuth exactly matches the callback URL used by the application.

### GitHub repository is empty

GitHub Releases require a repository with an initial commit. The application initializes a newly created empty repository before creating its first backup release.

### Backup job fails

Check the worker output for the actual backend error.

The web UI intentionally presents a generic backup-failure message rather than exposing backend details to the user.

### Upload verification fails

The application compares the SHA-256 digest calculated for the local TAR with the digest reported by GitHub. A mismatch causes the backup chunk to be rejected.

## Architecture

At a high level:

```text
                MyGoodOldPhotos
                       │
             ┌─────────┴─────────┐
             │                   │
         FastAPI             Worker
             │                   │
             └─────────┬─────────┘
                       │
                 Application DB
                       │
                       │
                       ▼
                 GitHub API
                       │
                       ▼
             Private GitHub Repo
                 + Releases
                 + TAR backups
```

The backend is responsible for authentication, application state, processing, and GitHub API interaction.

GitHub is the durable storage location for the actual backup files.

## Design principles

- Keep user backup media off permanent application storage.
- Never delete original data before successful upload and verification.
- Keep users' GitHub repositories isolated.
- Validate ownership of user resources.
- Do not expose secrets or access tokens.
- Keep temporary processing data temporary.
- Preserve the user's Google Photos library when deleting application backups.

## Hosting
- Render for application: https://render.com/ -> Enter all the secrets in its env variables and not in any files
- Aiven for DB: https://console.aiven.io/ -> Create a project for PostgreSQL eg CEO-rishikumar.
- google client ID: https://console.cloud.google.com/
- created one more github account which contains all the repositories eg CEO-rishikumar.
