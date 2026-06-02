from __future__ import annotations

import hmac
import json
import os
import sqlite3
import traceback
import uuid
from base64 import b64decode
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

import firebase_admin
from firebase_admin import credentials, firestore


APP_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = APP_ROOT / "data"
STATIC_DIR = APP_ROOT / "static"
SERVICE_ACCOUNT_PATH = APP_ROOT / "serviceAccountKey.json"
RUNNING_ON_VERCEL = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))
TEMP_DIR = Path(gettempdir())

SQLITE_PATH = Path(
    os.environ.get("SQLITE_PATH")
    or (
        TEMP_DIR / "memory_house.db"
        if RUNNING_ON_VERCEL
        else DATA_DIR / "memory_house.db"
    )
)
LOCAL_UPLOAD_DIR = Path(
    os.environ.get("LOCAL_UPLOAD_DIR")
    or (
        TEMP_DIR / "memory_house_uploads"
        if RUNNING_ON_VERCEL
        else STATIC_DIR / "uploads"
    )
)
LOCAL_UPLOAD_URL_PREFIX = os.environ.get("LOCAL_UPLOAD_URL_PREFIX") or (
    "/uploads" if RUNNING_ON_VERCEL else "/static/uploads"
)

DEFAULT_MAX_FILE_SIZE_MB = "4" if RUNNING_ON_VERCEL else "25"
DEFAULT_MAX_REQUEST_SIZE_MB = "4" if RUNNING_ON_VERCEL else "100"
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", DEFAULT_MAX_FILE_SIZE_MB))
MAX_REQUEST_SIZE_MB = int(
    os.environ.get("MAX_REQUEST_SIZE_MB", DEFAULT_MAX_REQUEST_SIZE_MB)
)
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

app = Flask(
    __name__,
    template_folder=str(APP_ROOT / "templates"),
    static_folder=str(STATIC_DIR),
    static_url_path="/static",
)
app.secret_key = os.environ.get("SECRET_KEY", "memory-house-secret")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_SIZE_MB * 1024 * 1024
app.jinja_env.auto_reload = True

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
FEATURED_LIMIT = 3
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()
ACCESS_CODE_HINT = os.environ.get("ACCESS_CODE_HINT", "").strip()

DEFAULT_SETTINGS = {
    "gallery_name": "Memory House",
    "home_kicker": "For my favorite girl",
    "partner_one": "Chel",
    "partner_two": "Ianna",
    "hero_heading": "For every ordinary day that turns unforgettable.",
    "hero_intro": (
        "A softer home for favorite photos, quiet milestones, and the little "
        "moments that end up meaning everything."
    ),
    "quote_text": "I have found the one whom my soul loves.",
    "quote_citation": "Song of Solomon 3:4",
    "relationship_start": "2025-01-14",
    "story_footer": "Built for the quiet, bright, ordinary kind of magic.",
    "story_note": (
        "The best love stories are usually made of tiny details: familiar laughs, "
        "favorite corners, and the way a normal day suddenly becomes worth saving."
    ),
}

DEFAULT_TIMELINE = [
    {
        "id": "connected",
        "badge": "The beginning",
        "title": "Got connected.",
        "description": "The first chapter landed quietly and changed everything after.",
        "event_date": "2025-01-14",
    },
    {
        "id": "official",
        "badge": "Something changed",
        "title": "We became official.",
        "description": "The page turned, and it started feeling like us for real.",
        "event_date": "2025-11-04",
    },
    {
        "id": "euphoria",
        "badge": "Core memory",
        "title": "Hung out at Euphoria and had so much fun.",
        "description": "The kind of date that deserves a permanent highlight on the wall.",
        "event_date": "2026-04-26",
    },
]

DEFAULT_LOVE_NOTES = [
    {
        "id": "laugh",
        "title": "Your laugh changes the weather.",
        "body": "It makes ordinary rooms feel brighter and ordinary days feel lighter.",
        "accent": "rose",
    },
    {
        "id": "peace",
        "title": "You make life feel softer.",
        "body": "Even the rushed days settle down a little when they end with you.",
        "accent": "gold",
    },
    {
        "id": "future",
        "title": "The future feels warmer with you in it.",
        "body": "This page is really just proof that loving you keeps making the world feel gentler.",
        "accent": "sage",
    },
]

FIREBASE_ERROR: str | None = None
FIRESTORE_REPOSITORY: FirestoreRepository | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_date_value(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    raw = str(value).strip()
    if not raw:
        return None
    if "T" in raw:
        raw = raw.split("T", 1)[0]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def format_date(value: Any, fallback: str = "") -> str:
    parsed = parse_date_value(value)
    if not parsed:
        return fallback or (str(value) if value else "")
    return parsed.strftime("%B %d, %Y").replace(" 0", " ")


def calculate_days_together(value: Any) -> int | None:
    parsed = parse_date_value(value)
    if not parsed:
        return None
    return max((date.today() - parsed).days, 0)


def build_version() -> str:
    template_file = APP_ROOT / "templates" / "index.html"
    try:
        template_mtime = datetime.fromtimestamp(
            template_file.stat().st_mtime, timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except OSError:
        template_mtime = "unknown"
    commit_sha = (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "local")[:7]
    return f"{commit_sha} | {template_mtime}"


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_file_size_bytes(file_storage) -> int:
    try:
        stream = file_storage.stream
        current_pos = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(current_pos)
        return size
    except Exception:
        return 0


def has_firebase_config() -> bool:
    return bool(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64")
        or SERVICE_ACCOUNT_PATH.exists()
    )


def normalize_storage_name(name: str | None, default: str) -> str:
    value = (name or "").strip().lower()
    return value if value in {"sqlite", "firestore", "local"} else default


def preferred_database_backend() -> str:
    explicit = normalize_storage_name(os.environ.get("DATABASE_BACKEND"), "")
    if explicit in {"sqlite", "firestore"}:
        return explicit
    if RUNNING_ON_VERCEL and has_firebase_config():
        return "firestore"
    return "sqlite"


def preferred_media_backend() -> str:
    return "local"


class SQLiteRepository:
    name = "sqlite"

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()
        self.seed_defaults()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def ensure_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS site_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS photos (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    storage_path TEXT,
                    public_id TEXT,
                    caption TEXT NOT NULL DEFAULT '',
                    section TEXT NOT NULL CHECK (section IN ('gallery', 'featured')),
                    slot_index INTEGER,
                    moment_date TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS timeline_events (
                    id TEXT PRIMARY KEY,
                    badge TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS love_notes (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    accent TEXT NOT NULL DEFAULT 'rose',
                    created_at TEXT NOT NULL
                );
                """
            )

    def seed_defaults(self) -> None:
        with self.connect() as conn:
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO site_settings (key, value)
                    VALUES (?, ?)
                    """,
                    (key, value),
                )

            timeline_count = conn.execute(
                "SELECT COUNT(*) AS count FROM timeline_events"
            ).fetchone()["count"]
            if not timeline_count:
                for item in DEFAULT_TIMELINE:
                    conn.execute(
                        """
                        INSERT INTO timeline_events (
                            id, badge, title, description, event_date, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item["id"],
                            item["badge"],
                            item["title"],
                            item["description"],
                            item["event_date"],
                            utc_now_iso(),
                        ),
                    )

            note_count = conn.execute(
                "SELECT COUNT(*) AS count FROM love_notes"
            ).fetchone()["count"]
            if not note_count:
                for item in DEFAULT_LOVE_NOTES:
                    conn.execute(
                        """
                        INSERT INTO love_notes (
                            id, title, body, accent, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            item["id"],
                            item["title"],
                            item["body"],
                            item["accent"],
                            utc_now_iso(),
                        ),
                    )

    def get_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM site_settings").fetchall()
        settings = {row["key"]: row["value"] for row in rows}
        return {**DEFAULT_SETTINGS, **settings}

    def update_settings(self, values: dict[str, str]) -> None:
        with self.connect() as conn:
            for key, value in values.items():
                conn.execute(
                    """
                    INSERT INTO site_settings (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )

    def list_photos(self, section: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM photos"
        params: list[Any] = []
        if section:
            query += " WHERE section = ?"
            params.append(section)
        if section == "featured":
            query += " ORDER BY COALESCE(slot_index, 999), datetime(created_at) DESC"
        else:
            query += " ORDER BY datetime(created_at) DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_photo(self, photo_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM photos WHERE id = ? LIMIT 1",
                (photo_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_photo(self, values: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO photos (
                    id, url, storage_path, public_id, caption, section,
                    slot_index, moment_date, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["id"],
                    values["url"],
                    values.get("storage_path"),
                    values.get("public_id"),
                    values.get("caption", ""),
                    values["section"],
                    values.get("slot_index"),
                    values.get("moment_date"),
                    values["created_at"],
                    values["updated_at"],
                ),
            )

    def update_photo(self, photo_id: str, values: dict[str, Any]) -> None:
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = list(values.values()) + [photo_id]
        with self.connect() as conn:
            conn.execute(
                f"UPDATE photos SET {assignments} WHERE id = ?",
                params,
            )

    def delete_photo(self, photo_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))

    def list_timeline_events(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM timeline_events
                ORDER BY date(event_date) ASC, datetime(created_at) ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_timeline_event(self, values: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO timeline_events (
                    id, badge, title, description, event_date, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    values["id"],
                    values["badge"],
                    values["title"],
                    values["description"],
                    values["event_date"],
                    values["created_at"],
                ),
            )

    def delete_timeline_event(self, event_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM timeline_events WHERE id = ?", (event_id,))

    def list_love_notes(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM love_notes ORDER BY datetime(created_at) DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_love_note(self, values: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO love_notes (id, title, body, accent, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    values["id"],
                    values["title"],
                    values["body"],
                    values["accent"],
                    values["created_at"],
                ),
            )

    def delete_love_note(self, note_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM love_notes WHERE id = ?", (note_id,))

    def export_data(self) -> dict[str, Any]:
        return {
            "settings": self.get_settings(),
            "timeline_events": self.list_timeline_events(),
            "love_notes": self.list_love_notes(),
            "photos": self.list_photos(),
        }


class FirestoreRepository:
    name = "firestore"

    def __init__(self) -> None:
        global FIREBASE_ERROR
        try:
            if not firebase_admin._apps:
                credentials_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
                if credentials_json:
                    cred = credentials.Certificate(json.loads(credentials_json))
                elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64"):
                    decoded = b64decode(
                        os.environ["GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64"]
                    ).decode("utf-8")
                    cred = credentials.Certificate(json.loads(decoded))
                elif SERVICE_ACCOUNT_PATH.exists():
                    cred = credentials.Certificate(str(SERVICE_ACCOUNT_PATH))
                else:
                    raise FileNotFoundError(
                        "Missing Firebase credentials. Set GOOGLE_APPLICATION_CREDENTIALS_JSON "
                        "or provide serviceAccountKey.json."
                    )
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            FIREBASE_ERROR = None
            self.seed_defaults()
        except Exception as exc:
            FIREBASE_ERROR = str(exc)
            raise

    @staticmethod
    def _normalize_document(document) -> dict[str, Any]:
        payload = document.to_dict() or {}
        payload["id"] = document.id
        for key in ("created_at", "updated_at"):
            value = payload.get(key)
            if isinstance(value, datetime):
                payload[key] = value.astimezone(timezone.utc).isoformat()
        return payload

    def seed_defaults(self) -> None:
        settings_ref = self.db.collection("site_settings").document("main")
        settings_doc = settings_ref.get()
        if not settings_doc.exists:
            settings_ref.set(DEFAULT_SETTINGS)

        timeline_docs = list(self.db.collection("timeline_events").limit(1).stream())
        if not timeline_docs:
            for item in DEFAULT_TIMELINE:
                self.db.collection("timeline_events").document(item["id"]).set(
                    {
                        **item,
                        "created_at": utc_now_iso(),
                    }
                )

        note_docs = list(self.db.collection("love_notes").limit(1).stream())
        if not note_docs:
            for item in DEFAULT_LOVE_NOTES:
                self.db.collection("love_notes").document(item["id"]).set(
                    {
                        **item,
                        "created_at": utc_now_iso(),
                    }
                )

    def get_settings(self) -> dict[str, str]:
        doc = self.db.collection("site_settings").document("main").get()
        payload = doc.to_dict() if doc.exists else {}
        return {**DEFAULT_SETTINGS, **(payload or {})}

    def update_settings(self, values: dict[str, str]) -> None:
        self.db.collection("site_settings").document("main").set(values, merge=True)

    def list_photos(self, section: str | None = None) -> list[dict[str, Any]]:
        docs = [
            self._normalize_document(document)
            for document in self.db.collection("photos").stream()
        ]
        if section:
            docs = [item for item in docs if item.get("section") == section]
        if section == "featured":
            docs.sort(
                key=lambda item: (
                    item.get("slot_index") if item.get("slot_index") is not None else 999,
                    item.get("created_at") or "",
                )
            )
        else:
            docs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return docs

    def get_photo(self, photo_id: str) -> dict[str, Any] | None:
        document = self.db.collection("photos").document(photo_id).get()
        if not document.exists:
            return None
        return self._normalize_document(document)

    def create_photo(self, values: dict[str, Any]) -> None:
        payload = dict(values)
        photo_id = payload.pop("id")
        self.db.collection("photos").document(photo_id).set(payload)

    def update_photo(self, photo_id: str, values: dict[str, Any]) -> None:
        self.db.collection("photos").document(photo_id).set(values, merge=True)

    def delete_photo(self, photo_id: str) -> None:
        self.db.collection("photos").document(photo_id).delete()

    def list_timeline_events(self) -> list[dict[str, Any]]:
        docs = [
            self._normalize_document(document)
            for document in self.db.collection("timeline_events").stream()
        ]
        docs.sort(key=lambda item: item.get("event_date") or "")
        return docs

    def create_timeline_event(self, values: dict[str, Any]) -> None:
        payload = dict(values)
        event_id = payload.pop("id")
        self.db.collection("timeline_events").document(event_id).set(payload)

    def delete_timeline_event(self, event_id: str) -> None:
        self.db.collection("timeline_events").document(event_id).delete()

    def list_love_notes(self) -> list[dict[str, Any]]:
        docs = [
            self._normalize_document(document)
            for document in self.db.collection("love_notes").stream()
        ]
        docs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return docs

    def create_love_note(self, values: dict[str, Any]) -> None:
        payload = dict(values)
        note_id = payload.pop("id")
        self.db.collection("love_notes").document(note_id).set(payload)

    def delete_love_note(self, note_id: str) -> None:
        self.db.collection("love_notes").document(note_id).delete()

    def export_data(self) -> dict[str, Any]:
        return {
            "settings": self.get_settings(),
            "timeline_events": self.list_timeline_events(),
            "love_notes": self.list_love_notes(),
            "photos": self.list_photos(),
        }


class LocalStorage:
    name = "local"

    def save(self, file_storage) -> dict[str, str | None]:
        original_name = secure_filename(file_storage.filename or "")
        suffix = Path(original_name).suffix.lower() or ".jpg"
        filename = f"{uuid.uuid4().hex}{suffix}"
        LOCAL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        destination = LOCAL_UPLOAD_DIR / filename
        file_storage.save(destination)
        return {
            "url": f"{LOCAL_UPLOAD_URL_PREFIX}/{filename}",
            "storage_path": filename,
            "public_id": None,
        }

    def delete(self, photo: dict[str, Any] | None) -> None:
        if not photo:
            return
        storage_path = photo.get("storage_path")
        if not storage_path:
            return
        target = LOCAL_UPLOAD_DIR / Path(storage_path).name
        if target.exists():
            target.unlink()


SQLITE_REPOSITORY: SQLiteRepository | None = None
LOCAL_STORAGE = LocalStorage()


def get_sqlite_repository() -> SQLiteRepository:
    global SQLITE_REPOSITORY
    if SQLITE_REPOSITORY is None:
        SQLITE_REPOSITORY = SQLiteRepository(SQLITE_PATH)
    return SQLITE_REPOSITORY


def resolve_repository() -> tuple[SQLiteRepository | FirestoreRepository, str, list[str]]:
    global FIRESTORE_REPOSITORY

    warnings: list[str] = []
    preferred = preferred_database_backend()
    if preferred == "firestore":
        try:
            if FIRESTORE_REPOSITORY is None:
                FIRESTORE_REPOSITORY = FirestoreRepository()
            return FIRESTORE_REPOSITORY, "firestore", warnings
        except Exception as exc:
            warnings.append(
                "Firestore could not be reached, so the app fell back to local SQLite. "
                f"Details: {exc}"
            )

    if RUNNING_ON_VERCEL:
        warnings.append(
            "SQLite is active on Vercel, so changes are temporary. Set "
            "`GOOGLE_APPLICATION_CREDENTIALS_JSON` (or the base64 variant) and "
            "`DATABASE_BACKEND=firestore` for durable online data."
        )
    return get_sqlite_repository(), "sqlite", warnings


def resolve_media_storage() -> tuple[LocalStorage, str, list[str]]:
    warnings: list[str] = []
    if RUNNING_ON_VERCEL:
        warnings.append(
            "Local uploads are temporary on Vercel because serverless files are not "
            "persistent. Run the site on this computer for durable local image storage."
        )
    return LOCAL_STORAGE, "local", warnings


def deployment_warnings() -> list[str]:
    if not RUNNING_ON_VERCEL:
        return []

    warnings: list[str] = []
    if app.secret_key == "memory-house-secret":
        warnings.append("Set `SECRET_KEY` in Vercel so sessions are private and stable.")
    if not ACCESS_CODE:
        warnings.append("Set `ACCESS_CODE` in Vercel if this gallery should stay private.")
    return warnings


def featured_slots_remaining(repository: SQLiteRepository | FirestoreRepository) -> list[int]:
    featured = repository.list_photos("featured")
    used = {item.get("slot_index") for item in featured if item.get("slot_index") in {1, 2, 3}}
    return [slot for slot in range(1, FEATURED_LIMIT + 1) if slot not in used]


def delete_photo_asset(photo: dict[str, Any] | None) -> None:
    if not photo:
        return
    storage_path = photo.get("storage_path")
    if not storage_path:
        return
    target = LOCAL_UPLOAD_DIR / Path(storage_path).name
    if target.exists():
        target.unlink()


def access_granted() -> bool:
    return not ACCESS_CODE or bool(session.get("gallery_access_ok"))


def access_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if access_granted():
            return view(*args, **kwargs)
        flash("Enter the access code first.", "error")
        return redirect(url_for("index"))

    return wrapped


@app.template_filter("pretty_date")
def pretty_date(value: Any) -> str:
    return format_date(value)


@app.route("/")
def index():
    repository, database_backend, repository_warnings = resolve_repository()
    media_storage, storage_backend, storage_warnings = resolve_media_storage()
    settings = repository.get_settings()
    warnings = repository_warnings + storage_warnings + deployment_warnings()
    locked = not access_granted()

    featured: list[dict[str, Any]] = []
    gallery: list[dict[str, Any]] = []
    timeline_events: list[dict[str, Any]] = []
    love_notes: list[dict[str, Any]] = []
    latest_caption = ""

    if not locked:
        try:
            featured = repository.list_photos("featured")[:FEATURED_LIMIT]
            gallery = repository.list_photos("gallery")
            timeline_events = repository.list_timeline_events()
            love_notes = repository.list_love_notes()
            latest_caption = next(
                (
                    item.get("caption", "").strip()
                    for item in [*gallery, *featured]
                    if item.get("caption", "").strip()
                ),
                "",
            )
        except Exception as exc:
            warnings.append(f"Some content could not be loaded: {exc}")
            print(f"INDEX ERROR: {exc}")
            traceback.print_exc()

    total_photos = len(featured) + len(gallery)
    days_together = calculate_days_together(settings.get("relationship_start"))
    runtime_stack = f"{database_backend.upper()} database + {storage_backend.upper()} media"

    return render_template(
        "index.html",
        settings=settings,
        featured=featured,
        gallery=gallery,
        total_photos=total_photos,
        featured_open_slots=max(FEATURED_LIMIT - len(featured), 0),
        latest_caption=latest_caption or settings.get("story_note", ""),
        timeline_events=timeline_events,
        love_notes=love_notes,
        days_together=days_together,
        max_file_size_mb=MAX_FILE_SIZE_MB,
        max_request_size_mb=MAX_REQUEST_SIZE_MB,
        is_locked=locked,
        access_code_enabled=bool(ACCESS_CODE),
        access_code_hint=ACCESS_CODE_HINT,
        runtime_stack=runtime_stack,
        database_backend=database_backend,
        storage_backend=storage_backend,
        warnings=warnings,
        firebase_ready=not repository_warnings,
        firebase_error=FIREBASE_ERROR or "",
        template_version=build_version(),
        can_edit=not locked,
        running_on_vercel=RUNNING_ON_VERCEL,
        upload_storage_label=(
            "temporary local storage" if RUNNING_ON_VERCEL else "this computer"
        ),
        today_iso=date.today().isoformat(),
    )


@app.route("/unlock", methods=["POST"])
def unlock():
    if not ACCESS_CODE:
        return redirect(url_for("index"))

    submitted_code = request.form.get("access_code", "").strip()
    if hmac.compare_digest(submitted_code, ACCESS_CODE):
        session["gallery_access_ok"] = True
        flash("Welcome back to your memory house.", "success")
    else:
        flash("That access code did not match.", "error")
    return redirect(url_for("index"))


@app.route("/lock", methods=["POST"])
def lock():
    session.pop("gallery_access_ok", None)
    flash("The gallery has been locked again.", "success")
    return redirect(url_for("index"))


@app.route("/story", methods=["POST"])
@access_required
def update_story():
    repository, _, _ = resolve_repository()
    payload = {
        key: request.form.get(key, "").strip()
        for key in DEFAULT_SETTINGS
    }
    if payload["relationship_start"] and not parse_date_value(payload["relationship_start"]):
        flash("Relationship start date should use the YYYY-MM-DD format.", "error")
        return redirect(url_for("index") + "#studio")

    repository.update_settings(
        {
            key: value or DEFAULT_SETTINGS[key]
            for key, value in payload.items()
        }
    )
    flash("The story details were updated.", "success")
    return redirect(url_for("index") + "#studio")


@app.route("/timeline", methods=["POST"])
@access_required
def add_timeline_event():
    repository, _, _ = resolve_repository()
    badge = request.form.get("badge", "").strip() or "Memory"
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    event_date = request.form.get("event_date", "").strip()

    if not title or not description or not event_date:
        flash("Timeline entries need a label, description, and date.", "error")
        return redirect(url_for("index") + "#studio")
    if not parse_date_value(event_date):
        flash("Timeline dates should use the YYYY-MM-DD format.", "error")
        return redirect(url_for("index") + "#studio")

    repository.create_timeline_event(
        {
            "id": uuid.uuid4().hex,
            "badge": badge,
            "title": title,
            "description": description,
            "event_date": event_date,
            "created_at": utc_now_iso(),
        }
    )
    flash("A new timeline moment was added.", "success")
    return redirect(url_for("index") + "#moments")


@app.route("/timeline/<event_id>/delete", methods=["POST"])
@access_required
def delete_timeline_event(event_id: str):
    repository, _, _ = resolve_repository()
    repository.delete_timeline_event(event_id)
    flash("Timeline moment deleted.", "success")
    return redirect(url_for("index") + "#moments")


@app.route("/notes", methods=["POST"])
@access_required
def add_love_note():
    repository, _, _ = resolve_repository()
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    accent = request.form.get("accent", "rose").strip().lower()

    if accent not in {"rose", "gold", "sage"}:
        accent = "rose"
    if not title or not body:
        flash("Love notes need both a title and a message.", "error")
        return redirect(url_for("index") + "#notes")

    repository.create_love_note(
        {
            "id": uuid.uuid4().hex,
            "title": title,
            "body": body,
            "accent": accent,
            "created_at": utc_now_iso(),
        }
    )
    flash("A new note was pinned to the page.", "success")
    return redirect(url_for("index") + "#notes")


@app.route("/notes/<note_id>/delete", methods=["POST"])
@access_required
def delete_love_note(note_id: str):
    repository, _, _ = resolve_repository()
    repository.delete_love_note(note_id)
    flash("Love note deleted.", "success")
    return redirect(url_for("index") + "#notes")


@app.route("/upload", methods=["POST"])
@access_required
def upload():
    repository, _, _ = resolve_repository()
    media_storage, _, _ = resolve_media_storage()

    files = [item for item in request.files.getlist("photo") if item and item.filename]
    caption = request.form.get("caption", "").strip()
    section = request.form.get("section", "gallery").strip().lower()
    moment_date = request.form.get("moment_date", "").strip()
    quiet_response = request.form.get("_async") == "1"

    if section not in {"gallery", "featured"}:
        section = "gallery"
    if moment_date and not parse_date_value(moment_date):
        flash("Moment date should use the YYYY-MM-DD format.", "error")
        return redirect(url_for("index"))
    if not files:
        if quiet_response:
            return Response("No files selected.", status=400)
        flash("Choose at least one photo first.", "error")
        return redirect(url_for("index"))

    open_slots = featured_slots_remaining(repository)
    uploaded_count = 0
    invalid_count = 0
    too_large_count = 0
    featured_full_count = 0
    failed_count = 0

    for file_storage in files:
        if not allowed_file(file_storage.filename):
            invalid_count += 1
            continue
        if get_file_size_bytes(file_storage) > MAX_FILE_SIZE_BYTES:
            too_large_count += 1
            continue
        if section == "featured" and not open_slots:
            featured_full_count += 1
            continue

        try:
            asset = media_storage.save(file_storage)
            photo_payload = {
                "id": uuid.uuid4().hex,
                "url": asset["url"],
                "storage_path": asset.get("storage_path"),
                "public_id": asset.get("public_id"),
                "caption": caption,
                "section": section,
                "slot_index": open_slots.pop(0) if section == "featured" else None,
                "moment_date": moment_date or None,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
            try:
                repository.create_photo(photo_payload)
            except Exception:
                delete_photo_asset(photo_payload)
                raise
            uploaded_count += 1
        except Exception as exc:
            failed_count += 1
            print(f"UPLOAD ERROR: {exc}")
            traceback.print_exc()

    if quiet_response:
        if uploaded_count and not any(
            [invalid_count, too_large_count, featured_full_count, failed_count]
        ):
            return Response(status=204)
        return Response("Upload failed.", status=400)

    if uploaded_count:
        flash(f"{uploaded_count} photo(s) saved.", "success")
    if invalid_count:
        flash(f"{invalid_count} file(s) were skipped because the format is not supported.", "error")
    if too_large_count:
        flash(
            f"{too_large_count} file(s) were skipped because each photo must be {MAX_FILE_SIZE_MB}MB or less.",
            "error",
        )
    if featured_full_count:
        flash(
            "The featured wall already has three photos, so extra featured uploads were skipped.",
            "error",
        )
    if failed_count:
        flash("Some uploads failed. Try smaller files if you are deploying online.", "error")
    return redirect(url_for("index"))


@app.route("/replace/<photo_id>", methods=["POST"])
@access_required
def replace(photo_id: str):
    repository, _, _ = resolve_repository()
    media_storage, _, _ = resolve_media_storage()

    file_storage = request.files.get("photo")
    if not file_storage or not file_storage.filename:
        flash("Choose a replacement photo first.", "error")
        return redirect(url_for("index"))
    if not allowed_file(file_storage.filename):
        flash("That file type is not supported.", "error")
        return redirect(url_for("index"))
    if get_file_size_bytes(file_storage) > MAX_FILE_SIZE_BYTES:
        flash(f"Replacement photos must be {MAX_FILE_SIZE_MB}MB or less.", "error")
        return redirect(url_for("index"))

    existing = repository.get_photo(photo_id)
    if not existing:
        flash("That photo could not be found.", "error")
        return redirect(url_for("index"))

    try:
        asset = media_storage.save(file_storage)
        try:
            repository.update_photo(
                photo_id,
                {
                    "url": asset["url"],
                    "storage_path": asset.get("storage_path"),
                    "public_id": asset.get("public_id"),
                    "updated_at": utc_now_iso(),
                },
            )
        except Exception:
            delete_photo_asset(asset)
            raise
        delete_photo_asset(existing)
        flash("Photo replaced.", "success")
    except Exception as exc:
        print(f"REPLACE ERROR: {exc}")
        traceback.print_exc()
        flash("The photo could not be replaced.", "error")

    return redirect(url_for("index"))


@app.route("/delete/<photo_id>", methods=["POST"])
@access_required
def delete(photo_id: str):
    repository, _, _ = resolve_repository()
    media_storage, _, _ = resolve_media_storage()

    photo = repository.get_photo(photo_id)
    if not photo:
        flash("That photo could not be found.", "error")
        return redirect(url_for("index"))

    try:
        repository.delete_photo(photo_id)
        delete_photo_asset(photo)
        flash("Photo deleted.", "success")
    except Exception as exc:
        print(f"DELETE ERROR: {exc}")
        flash("The photo could not be deleted.", "error")
    return redirect(url_for("index"))


@app.route("/backup.json")
@access_required
def backup():
    repository, database_backend, _ = resolve_repository()
    _, storage_backend, _ = resolve_media_storage()
    payload = repository.export_data()
    payload["exported_at"] = utc_now_iso()
    payload["database_backend"] = database_backend
    payload["storage_backend"] = storage_backend
    filename = f"memory-house-backup-{date.today().isoformat()}.json"
    response = Response(
        json.dumps(payload, indent=2, ensure_ascii=True),
        mimetype="application/json",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@app.route("/favicon.svg")
def favicon():
    public_dir = APP_ROOT / "public"
    return send_from_directory(public_dir, "favicon.svg")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(LOCAL_UPLOAD_DIR, Path(filename).name)


@app.after_request
def add_no_cache_headers(response):
    if response.content_type and response.content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_error):
    flash(
        f"Upload payload too large. Keep each batch under {MAX_REQUEST_SIZE_MB}MB.",
        "error",
    )
    return redirect(url_for("index"))
