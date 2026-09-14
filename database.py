from __future__ import annotations

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, synonym


# ============================================================
# Environment
# ============================================================

load_dotenv()


# ============================================================
# Database configuration
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if DATABASE_URL:
    DB_URL = DATABASE_URL
else:
    DB_URL = "sqlite:///./mygoodoldphotos.db"


connect_args = {}

if DB_URL.startswith("sqlite"):
    connect_args = {
        "check_same_thread": False,
    }


engine = create_engine(
    DB_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)


SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


Base = declarative_base()


# ============================================================
# Helpers
# ============================================================

def utcnow():
    return datetime.now(timezone.utc)


# ============================================================
# User
# ============================================================

class User(Base):
    __tablename__ = "users"

    id = Column(
        Integer,
        primary_key=True,
    )

    # Google OpenID subject identifier.
    # This is the stable identity used by OAuth login.
    google_subject_id = Column(
        String(255),
        nullable=True,
        index=True,
    )

    email = Column(
        String(320),
        nullable=False,
        index=True,
    )

    email_prefix = Column(
        String(255),
        nullable=False,
    )

    # Keep the historical DB column name used by the existing application.
    name = Column(
        String(255),
        nullable=True,
    )

    # Current application code uses display_name.
    # Map it to the existing "name" database column.
    display_name = synonym("name")

    storage_mode = Column(
        String(32),
        nullable=False,
        default="platform",
    )

    encrypted_google_token = Column(
        Text,
        nullable=True,
    )

    encrypted_github_token = Column(
        Text,
        nullable=True,
    )

    github_username = Column(
        String(255),
        nullable=True,
    )

    github_repo_name = Column(
        String(255),
        nullable=True,
    )

    github_repo_id = Column(
        Integer,
        nullable=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    albums = relationship(
        "Album",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    backup_jobs = relationship(
        "BackupJob",
        back_populates="user",
        cascade="all, delete-orphan",
    )


# ============================================================
# Album
# ============================================================

class Album(Base):
    __tablename__ = "albums"

    id = Column(
        Integer,
        primary_key=True,
    )

    user_id = Column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    name = Column(
        String(500),
        nullable=False,
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    user = relationship(
        "User",
        back_populates="albums",
    )

    backup_jobs = relationship(
        "BackupJob",
        back_populates="album",
        cascade="all, delete-orphan",
    )

    chunks = relationship(
        "Chunk",
        back_populates="album",
        cascade="all, delete-orphan",
    )


# ============================================================
# Backup Job
# ============================================================

class BackupJob(Base):
    __tablename__ = "backup_jobs"

    id = Column(
        Integer,
        primary_key=True,
    )

    user_id = Column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    album_id = Column(
        Integer,
        ForeignKey(
            "albums.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    status = Column(
        String(32),
        nullable=False,
        default="queued",
        index=True,
    )

    # Explicitly records the request source; do not infer ownership or state
    # from browser input.  ``picker_session_id`` remains the durable Picker
    # handle for Google jobs.
    source = Column(
        String(32),
        nullable=False,
        default="local",
        index=True,
    )

    total_items = Column(
        Integer,
        nullable=False,
        default=0,
    )

    total_bytes = Column(Integer, nullable=False, default=0)
    processed_bytes = Column(Integer, nullable=False, default=0)
    current_bytes = Column(Integer, nullable=False, default=0)
    current_total_bytes = Column(Integer, nullable=False, default=0)
    current_filename = Column(String(1000), nullable=True)
    current_operation = Column(String(64), nullable=True)
    user_message = Column(Text, nullable=True)

    completed_items = Column(
        Integer,
        nullable=False,
        default=0,
    )

    current_part = Column(
        Integer,
        nullable=False,
        default=0,
    )

    error = Column(
        Text,
        nullable=True,
    )

    # Google Photos Picker session.
    # NULL means this is a local-upload job.
    picker_session_id = Column(
        String(255),
        nullable=True,
        index=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    started_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    completed_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    user = relationship(
        "User",
        back_populates="backup_jobs",
    )

    album = relationship(
        "Album",
        back_populates="backup_jobs",
    )

    media_items = relationship(
        "MediaItem",
        back_populates="job",
        cascade="all, delete-orphan",
    )


# ============================================================
# Media Item
# ============================================================

class MediaItem(Base):
    __tablename__ = "media_items"

    id = Column(
        Integer,
        primary_key=True,
    )

    job_id = Column(
        Integer,
        ForeignKey(
            "backup_jobs.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    # Google media ID for Google Photos jobs.
    # Local uploads use a synthetic local:<job>:<uuid> ID.
    google_media_id = Column(
        String(500),
        nullable=True,
        index=True,
    )

    source_base_url = Column(
        Text,
        nullable=True,
    )

    filename = Column(
        String(1000),
        nullable=False,
    )

    mime_type = Column(
        String(255),
        nullable=True,
    )

    size = Column(
        Integer,
        nullable=False,
        default=0,
    )

    sha256 = Column(
        String(64),
        nullable=True,
        index=True,
    )

    local_path = Column(
        Text,
        nullable=True,
    )

    status = Column(
        String(32),
        nullable=False,
        default="pending",
        index=True,
    )

    part_number = Column(
        Integer,
        nullable=True,
    )

    asset_name = Column(
        String(1000),
        nullable=True,
    )

    release_tag = Column(
        String(255),
        nullable=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    job = relationship(
        "BackupJob",
        back_populates="media_items",
    )


# ============================================================
# Chunk
# ============================================================

class Chunk(Base):
    __tablename__ = "chunks"

    id = Column(
        Integer,
        primary_key=True,
    )

    album_id = Column(
        Integer,
        ForeignKey(
            "albums.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    part_number = Column(
        Integer,
        nullable=False,
        index=True,
    )

    asset_name = Column(
        String(1000),
        nullable=False,
    )

    github_asset_id = Column(
        Integer,
        nullable=False,
        index=True,
    )

    github_release_id = Column(
        Integer,
        nullable=False,
        index=True,
    )

    release_tag = Column(
        String(255),
        nullable=False,
        index=True,
    )

    size = Column(
        Integer,
        nullable=False,
        default=0,
    )

    sha256 = Column(
        String(64),
        nullable=False,
        index=True,
    )

    status = Column(
        String(32),
        nullable=False,
        default="uploaded",
        index=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    album = relationship(
        "Album",
        back_populates="chunks",
    )


# ============================================================
# Existing SQLite migration support
# ============================================================

def _sqlite_add_missing_columns():
    """
    Add columns introduced after the initial SQLite schema.

    SQLAlchemy create_all() does not alter existing tables, so
    older local databases need this small compatibility migration.
    """

    if not DB_URL.startswith("sqlite"):
        return

    inspector = inspect(engine)

    table_columns = {
        table_name: {
            column["name"]
            for column in inspector.get_columns(table_name)
        }
        for table_name in inspector.get_table_names()
    }

    migrations = {
        "users": {
            "google_subject_id": (
                "VARCHAR(255)"
            ),
            "name": (
                "VARCHAR(255)"
            ),
            "updated_at": (
                "DATETIME"
            ),
        },
        "backup_jobs": {
            "total_bytes": ("INTEGER NOT NULL DEFAULT 0"),
            "processed_bytes": ("INTEGER NOT NULL DEFAULT 0"),
            "current_bytes": ("INTEGER NOT NULL DEFAULT 0"),
            "current_total_bytes": ("INTEGER NOT NULL DEFAULT 0"),
            "current_filename": ("VARCHAR(1000)"),
            "current_operation": ("VARCHAR(64)"),
            "user_message": ("TEXT"),
            "source": (
                "VARCHAR(32) NOT NULL DEFAULT 'local'"
            ),
            "picker_session_id": (
                "VARCHAR(255)"
            ),
            "started_at": (
                "DATETIME"
            ),
            "completed_at": (
                "DATETIME"
            ),
        },
        "media_items": {
            "source_base_url": (
                "TEXT"
            ),
        },
    }

    with engine.begin() as connection:
        for table_name, columns in migrations.items():

            if table_name not in table_columns:
                continue

            existing = table_columns[
                table_name
            ]

            for column_name, column_type in columns.items():

                if column_name in existing:
                    continue

                connection.execute(
                    text(
                        f'ALTER TABLE "{table_name}" '
                        f'ADD COLUMN "{column_name}" '
                        f"{column_type}"
                    )
                )


def _ensure_indexes():
    """
    Create useful indexes for existing databases without
    requiring a full Alembic migration system yet.
    """

    with engine.begin() as connection:

        statements = [
            (
                "CREATE INDEX IF NOT EXISTS "
                "ix_users_google_subject_id "
                "ON users (google_subject_id)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS "
                "ix_backup_jobs_picker_session_id "
                "ON backup_jobs (picker_session_id)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS "
                "ix_media_items_google_media_id "
                "ON media_items (google_media_id)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS "
                "ix_media_items_sha256 "
                "ON media_items (sha256)"
            ),
        ]

        for statement in statements:
            try:
                connection.execute(
                    text(statement)
                )
            except Exception:
                # Some older schemas may not yet have a table.
                # create_all() below will handle new installations.
                pass


# ============================================================
# Database initialization
# ============================================================

def init_db():
    """
    Create all missing tables and migrate lightweight SQLite
    schema additions required by the current application.
    """

    Base.metadata.create_all(
        bind=engine
    )

    _sqlite_add_missing_columns()
    _ensure_indexes()
    if DB_URL.startswith("sqlite"):
        with engine.begin() as connection:
            connection.execute(text("UPDATE backup_jobs SET source='google' WHERE picker_session_id IS NOT NULL AND source='local'"))
    elif DB_URL.startswith(("postgresql", "postgres")):
        # Compatibility migration for installations created before job source
        # was explicit. PostgreSQL supports IF NOT EXISTS atomically.
        with engine.begin() as connection:
            postgres_columns = {
                "source": "VARCHAR(32) NOT NULL DEFAULT 'local'",
                "picker_session_id": "VARCHAR(255)",
                "started_at": "TIMESTAMP WITH TIME ZONE",
                "completed_at": "TIMESTAMP WITH TIME ZONE",
                "total_bytes": "BIGINT NOT NULL DEFAULT 0",
                "processed_bytes": "BIGINT NOT NULL DEFAULT 0",
                "current_bytes": "BIGINT NOT NULL DEFAULT 0",
                "current_total_bytes": "BIGINT NOT NULL DEFAULT 0",
                "current_filename": "VARCHAR(1000)",
                "current_operation": "VARCHAR(64)",
                "user_message": "TEXT",
            }
            for column_name, column_type in postgres_columns.items():
                connection.execute(text(
                    f"ALTER TABLE backup_jobs ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
                ))
            connection.execute(text("UPDATE backup_jobs SET source='google' WHERE picker_session_id IS NOT NULL AND source='local'"))


def recover_stale_jobs(max_age_seconds: int = 3600) -> int:
    """Return abandoned worker claims to the queue after a process crash."""
    from datetime import timedelta

    cutoff = utcnow() - timedelta(seconds=max_age_seconds)
    db = SessionLocal()
    try:
        rows = (db.query(BackupJob).filter(BackupJob.status == "running",
                                           BackupJob.started_at < cutoff).all())
        for job in rows:
            job.status = "queued"
            job.error = "Recovered after an interrupted worker run."
            job.started_at = None
        db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ============================================================
# FastAPI dependency helper
# ============================================================

def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


# ============================================================
# Direct execution
# ============================================================

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
