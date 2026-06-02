import os
import json
import sqlite3
import uuid
import traceback
from datetime import datetime, timezone

from flask import Flask, Response, render_template, request, redirect, url_for, flash, send_from_directory
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import FieldFilter

try:
    import cloudinary
    import cloudinary.uploader
except ImportError:
    cloudinary = None

# ── Flask ──
app = Flask(
    __name__,
    template_folder="../templates",
    static_folder="../static",
    static_url_path="/static"
)
app.secret_key = os.environ.get("SECRET_KEY") or "*qaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqa"
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
RUNNING_ON_VERCEL = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))
DEFAULT_MAX_FILE_SIZE_MB = "3" if RUNNING_ON_VERCEL else "25"
DEFAULT_MAX_REQUEST_SIZE_MB = "4" if RUNNING_ON_VERCEL else "100"
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", DEFAULT_MAX_FILE_SIZE_MB))
MAX_REQUEST_SIZE_MB = int(os.environ.get("MAX_REQUEST_SIZE_MB", DEFAULT_MAX_REQUEST_SIZE_MB))
if RUNNING_ON_VERCEL:
    MAX_FILE_SIZE_MB = min(MAX_FILE_SIZE_MB, 3)
    MAX_REQUEST_SIZE_MB = min(MAX_REQUEST_SIZE_MB, 4)
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
FIREBASE_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("FIREBASE_REQUEST_TIMEOUT_SECONDS", "10"))
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_SIZE_MB * 1024 * 1024


APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOCAL_DB_PATH = os.environ.get("SQLITE_PATH") or os.path.join(APP_ROOT, "data", "memory_house.db")
LOCAL_UPLOAD_DIR = os.environ.get("LOCAL_UPLOAD_DIR") or os.path.join(APP_ROOT, "static", "uploads")
db = None
firebase_init_error = None
cloudinary_configured = False
cloudinary_init_error = None
CLOUDINARY_UPLOAD_FOLDER = os.environ.get("CLOUDINARY_UPLOAD_FOLDER", "couple_gallery").strip().strip("/") or "couple_gallery"


def parse_labeled_secret_line(line):
    cleaned = line.strip()
    lowered = cleaned.lower()
    for label in ("cloud name", "api key", "api secret"):
        if lowered.startswith(label):
            return cleaned[len(label):].strip(" :=\t")
    if ":" in line:
        return line.split(":", 1)[1].strip()
    return cleaned


def load_local_cloudinary_settings():
    path = os.path.join(APP_ROOT, "cloudinary.txt")
    if not os.path.exists(path):
        return {}

    try:
        lines = [
            line.strip()
            for line in open(path, encoding="utf-8").read().splitlines()
            if line.strip()
        ]
    except Exception:
        return {}

    settings = {}
    for line in lines:
        if line.startswith("cloudinary://"):
            settings["CLOUDINARY_URL"] = line
        elif "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip()

    # Support the local note format:
    # Cloud Name / <cloud name> / API Key: <key> / API Secret: <secret>
    if not settings and len(lines) >= 4 and lines[0].lower().replace(" ", "") == "cloudname":
        settings["CLOUDINARY_CLOUD_NAME"] = lines[1].strip()
        settings["CLOUDINARY_API_KEY"] = parse_labeled_secret_line(lines[2])
        settings["CLOUDINARY_API_SECRET"] = parse_labeled_secret_line(lines[3])

    return settings


LOCAL_CLOUDINARY_SETTINGS = load_local_cloudinary_settings()


def cloudinary_setting(name):
    return os.environ.get(name) or LOCAL_CLOUDINARY_SETTINGS.get(name, "")


CLOUDINARY_URL = cloudinary_setting("CLOUDINARY_URL").strip()
CLOUDINARY_CLOUD_NAME = cloudinary_setting("CLOUDINARY_CLOUD_NAME").strip()
CLOUDINARY_API_KEY = cloudinary_setting("CLOUDINARY_API_KEY").strip()
CLOUDINARY_API_SECRET = cloudinary_setting("CLOUDINARY_API_SECRET").strip()
SCRAPBOOK_EDIT_KEY = os.environ.get("SCRAPBOOK_EDIT_KEY", "").strip()


def has_cloudinary_credentials():
    return bool(
        CLOUDINARY_URL
        or (CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET)
    )


def use_local_backend():
    if RUNNING_ON_VERCEL:
        return False
    database_backend = os.environ.get("DATABASE_BACKEND", "").strip().lower()
    storage_backend = os.environ.get("STORAGE_BACKEND", "").strip().lower()
    if database_backend == "sqlite" or storage_backend == "local":
        return True
    if storage_backend == "cloudinary":
        return False
    if cloudinary is None:
        return True
    return not has_cloudinary_credentials()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_photo_section(section):
    return section if section in {"gallery", "featured", "scrapbook"} else "gallery"


DEFAULT_SCRAPBOOK_PAGES = [
    {
        "id": "first-spark",
        "number": "01",
        "story_title": "The First Spark",
        "story_event": "Got connected.",
        "story_note": "January 14, 2025 - a simple beginning that quietly changed the shape of everything after it.",
        "page_date": "January 14, 2025",
        "page_title": "The first spark",
        "page_note": "The beginning gets its own page because small starts can become the softest forever things.",
        "is_core": False,
    },
    {
        "id": "the-choice",
        "number": "02",
        "story_title": "The Choice",
        "story_event": "We became official.",
        "story_note": "November 4, 2025 - the moment it stopped being maybe and started feeling like home.",
        "page_date": "November 4, 2025",
        "page_title": "The choice",
        "page_note": "The day the maybe became real, and the story started sounding a lot like home.",
        "is_core": False,
    },
    {
        "id": "euphoria-day",
        "number": "03",
        "story_title": "The Joy",
        "story_event": "Euphoria became a core memory.",
        "story_note": "April 26, 2026 - one of those days that proves love is also laughter, lightness, and being fully yourself together.",
        "page_date": "April 26, 2026",
        "page_title": "Euphoria day",
        "page_note": "A page for laughter, brightness, and the kind of fun that becomes a tiny private universe.",
        "is_core": True,
    },
    {
        "id": "right-now",
        "number": "04",
        "story_title": "The Now",
        "story_event": "Still choosing each other.",
        "story_note": "Not perfect, not performative - just two people building something gentle, steady, and real.",
        "page_date": "Right now",
        "page_title": "Still choosing us",
        "page_note": "The newest page is for every ordinary day that quietly becomes another reason to stay.",
        "is_core": False,
    },
]
SCRAPBOOK_PAGE_IDS = {page["id"] for page in DEFAULT_SCRAPBOOK_PAGES}
SCRAPBOOK_PAGE_FIELDS = (
    "story_title",
    "story_event",
    "story_note",
    "page_date",
    "page_title",
    "page_note",
)


def sanitize_text(value, max_length=500):
    text = (value or "").strip()
    return text[:max_length]


def scrapbook_page_defaults():
    return [dict(page) for page in DEFAULT_SCRAPBOOK_PAGES]


def merge_scrapbook_pages(saved_pages):
    saved_by_id = {
        page.get("id"): page
        for page in (saved_pages or [])
        if page.get("id") in SCRAPBOOK_PAGE_IDS
    }
    pages = []
    for default_page in scrapbook_page_defaults():
        saved = saved_by_id.get(default_page["id"], {})
        merged = {**default_page}
        for field in SCRAPBOOK_PAGE_FIELDS:
            value = sanitize_text(saved.get(field), 800 if field.endswith("_note") else 120)
            if value:
                merged[field] = value
        pages.append(merged)
    return pages


def scrapbook_page_form_values(form):
    return {
        "story_title": sanitize_text(form.get("story_title"), 120),
        "story_event": sanitize_text(form.get("story_event"), 180),
        "story_note": sanitize_text(form.get("story_note"), 800),
        "page_date": sanitize_text(form.get("page_date"), 120),
        "page_title": sanitize_text(form.get("page_title"), 120),
        "page_note": sanitize_text(form.get("page_note"), 800),
        "updated_at": utc_now_iso(),
    }


def scrapbook_edit_value():
    return SCRAPBOOK_EDIT_KEY or "1"


def scrapbook_edit_allowed():
    supplied_key = (
        request.values.get("edit_key")
        or request.args.get("edit")
        or request.form.get("edit")
        or ""
    ).strip()
    if SCRAPBOOK_EDIT_KEY:
        return supplied_key == SCRAPBOOK_EDIT_KEY
    return supplied_key == "1"


# Firebase handles Firestore metadata. Cloudinary handles durable image storage.
def init_firebase():
    global db, firebase_init_error

    if db is not None:
        return db

    try:
        if not firebase_admin._apps:
            cred_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
            if cred_json:
                cred_dict = json.loads(cred_json)
                cred = credentials.Certificate(cred_dict)
            else:
                key_path = os.path.abspath(
                    os.path.join(os.path.dirname(__file__), "..", "serviceAccountKey.json")
                )
                if not os.path.exists(key_path):
                    raise FileNotFoundError(
                        "Missing Firebase credentials. Set GOOGLE_APPLICATION_CREDENTIALS_JSON in Vercel."
                    )
                cred = credentials.Certificate(key_path)
            firebase_admin.initialize_app(cred)

        db = firestore.client()
        firebase_init_error = None
    except Exception as exc:
        firebase_init_error = str(exc)
        db = None
        print(f"FIREBASE INIT ERROR: {firebase_init_error}")

    return db


def init_cloudinary():
    global cloudinary_configured, cloudinary_init_error

    if cloudinary_configured:
        return True
    if cloudinary is None:
        cloudinary_init_error = "Missing Cloudinary package. Add cloudinary to requirements.txt."
        return False
    if not has_cloudinary_credentials():
        cloudinary_init_error = (
            "Missing Cloudinary credentials. Set CLOUDINARY_URL or "
            "CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET."
        )
        return False

    try:
        if CLOUDINARY_URL and not os.environ.get("CLOUDINARY_URL"):
            os.environ["CLOUDINARY_URL"] = CLOUDINARY_URL

        if CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
            cloudinary.config(
                cloud_name=CLOUDINARY_CLOUD_NAME,
                api_key=CLOUDINARY_API_KEY,
                api_secret=CLOUDINARY_API_SECRET,
                secure=True,
            )
        else:
            cloudinary.config(secure=True)

        config = cloudinary.config()
        if not (config.cloud_name and config.api_key and config.api_secret):
            raise RuntimeError("Cloudinary credentials are incomplete.")

        cloudinary_configured = True
        cloudinary_init_error = None
        return True
    except Exception as exc:
        cloudinary_configured = False
        cloudinary_init_error = str(exc)
        print(f"CLOUDINARY INIT ERROR: {cloudinary_init_error}")
        return False


def storage_ready():
    return init_cloudinary()


def cloudinary_folder(section):
    section = normalize_photo_section(section)
    return f"{CLOUDINARY_UPLOAD_FOLDER}/{section}".strip("/")


def friendly_upload_error(error):
    message = str(error)
    lowered = message.lower()

    if "cloudinary" in lowered and ("missing" in lowered or "incomplete" in lowered):
        return (
            "Cloudinary is not configured. Set CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET in Vercel."
        )
    if "must supply api_key" in lowered or "must supply api_secret" in lowered:
        return "Cloudinary credentials are incomplete. Recheck the Vercel env vars."
    if "401" in lowered or "unauthorized" in lowered or "invalid api" in lowered:
        return "Cloudinary rejected the credentials. Recheck the API key and API secret."
    if "permission" in lowered or "403" in lowered or "forbidden" in lowered:
        return (
            "Cloudinary permission denied. Check the Cloudinary account/API permissions."
        )
    if "credentials" in lowered or "private_key" in lowered or "invalid json" in lowered:
        return (
            "Firebase credentials are invalid. Recheck GOOGLE_APPLICATION_CREDENTIALS_JSON "
            "in Vercel."
        )
    if "file size" in lowered or "too large" in lowered or "payload" in lowered:
        return "Photo is too large for this upload. Try one smaller photo."
    if "timeout" in lowered or "timed out" in lowered:
        return "Cloudinary upload timed out. Try one smaller photo, then upload again."
    if message:
        return f"Upload failed: {message[:220]}"
    return "Upload failed. Check the Vercel function logs for details."


def upload_to_cloudinary(file_storage, section="gallery"):
    if not init_cloudinary():
        raise RuntimeError(cloudinary_init_error or "Cloudinary is not configured.")

    original_name = secure_filename(file_storage.filename or "photo")
    file_storage.stream.seek(0)

    result = cloudinary.uploader.upload(
        file_storage.stream,
        folder=cloudinary_folder(section),
        resource_type="image",
        use_filename=bool(original_name),
        filename_override=original_name or None,
        unique_filename=True,
        overwrite=False,
    )
    public_id = result.get("public_id")
    secure_url = result.get("secure_url") or result.get("url")
    if not public_id or not secure_url:
        raise RuntimeError("Cloudinary upload did not return a public URL.")

    return {
        "url": secure_url,
        "storage_path": public_id,
        "public_id": public_id,
    }


def delete_cloudinary_asset(public_id):
    if not public_id or not init_cloudinary():
        return
    try:
        cloudinary.uploader.destroy(public_id, resource_type="image", invalidate=True)
    except Exception as exc:
        # Keep Firestore cleanup successful even if an old asset is already gone.
        print(f"CLOUDINARY DELETE WARNING: {exc}")


def local_connect():
    os.makedirs(os.path.dirname(LOCAL_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(LOCAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def local_photo_columns(conn, table_name="photos"):
    return [
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    ]


def create_local_photos_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            storage_path TEXT,
            public_id TEXT,
            caption TEXT NOT NULL DEFAULT '',
            section TEXT NOT NULL DEFAULT 'gallery',
            slot_index INTEGER,
            moment_date TEXT,
            uploaded_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )


def create_local_scrapbook_pages_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scrapbook_pages (
            id TEXT PRIMARY KEY,
            story_title TEXT,
            story_event TEXT,
            story_note TEXT,
            page_date TEXT,
            page_title TEXT,
            page_note TEXT,
            updated_at TEXT
        )
        """
    )


def migrate_local_photos_table(conn):
    old_table = f"photos_legacy_{uuid.uuid4().hex[:8]}"
    conn.execute(f"ALTER TABLE photos RENAME TO {old_table}")
    create_local_photos_table(conn)

    columns = local_photo_columns(conn, old_table)
    rows = conn.execute(f"SELECT * FROM {old_table}").fetchall()
    for row in rows:
        data = {column: row[column] for column in columns}
        url = data.get("url") or ""
        if not url:
            continue
        created_at = data.get("created_at") or data.get("uploaded_at") or utc_now_iso()
        updated_at = data.get("updated_at") or created_at
        uploaded_at = data.get("uploaded_at") or created_at
        conn.execute(
            """
            INSERT OR REPLACE INTO photos (
                id, url, storage_path, public_id, caption, section, slot_index,
                moment_date, uploaded_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("id") or uuid.uuid4().hex,
                url,
                data.get("storage_path"),
                data.get("public_id"),
                data.get("caption") or "",
                normalize_photo_section(data.get("section")),
                data.get("slot_index"),
                data.get("moment_date"),
                uploaded_at,
                created_at,
                updated_at,
            ),
        )


def ensure_local_schema():
    with local_connect() as conn:
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'photos'"
        ).fetchone()
        if table is None:
            create_local_photos_table(conn)
            create_local_scrapbook_pages_table(conn)
            return

        create_sql = table["sql"] or ""
        if "CHECK" in create_sql.upper() and "SCRAPBOOK" not in create_sql.upper():
            migrate_local_photos_table(conn)
            create_local_scrapbook_pages_table(conn)
            return

        existing = set(local_photo_columns(conn))
        missing_columns = {
            "storage_path": "TEXT",
            "public_id": "TEXT",
            "caption": "TEXT NOT NULL DEFAULT ''",
            "section": "TEXT NOT NULL DEFAULT 'gallery'",
            "slot_index": "INTEGER",
            "moment_date": "TEXT",
            "uploaded_at": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        }
        for column, definition in missing_columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE photos ADD COLUMN {column} {definition}")
        create_local_scrapbook_pages_table(conn)


def list_local_photos(section):
    ensure_local_schema()
    with local_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM photos
            WHERE section = ?
            ORDER BY COALESCE(uploaded_at, created_at, updated_at, '') DESC
            """,
            (section,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_local_photo(photo_id):
    ensure_local_schema()
    with local_connect() as conn:
        row = conn.execute(
            "SELECT * FROM photos WHERE id = ? LIMIT 1",
            (photo_id,),
        ).fetchone()
    return dict(row) if row else None


def create_local_photo(values):
    ensure_local_schema()
    with local_connect() as conn:
        conn.execute(
            """
            INSERT INTO photos (
                id, url, storage_path, public_id, caption, section,
                uploaded_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["id"],
                values["url"],
                values.get("storage_path"),
                values.get("public_id"),
                values.get("caption", ""),
                normalize_photo_section(values.get("section")),
                values.get("uploaded_at"),
                values.get("created_at"),
                values.get("updated_at"),
            ),
        )


def update_local_photo(photo_id, values):
    ensure_local_schema()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with local_connect() as conn:
        conn.execute(
            f"UPDATE photos SET {assignments} WHERE id = ?",
            [*values.values(), photo_id],
        )


def delete_local_photo(photo_id):
    ensure_local_schema()
    with local_connect() as conn:
        conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))


def list_local_scrapbook_pages():
    ensure_local_schema()
    with local_connect() as conn:
        rows = conn.execute("SELECT * FROM scrapbook_pages").fetchall()
    return merge_scrapbook_pages([dict(row) for row in rows])


def update_local_scrapbook_page(page_id, values):
    ensure_local_schema()
    with local_connect() as conn:
        conn.execute(
            """
            INSERT INTO scrapbook_pages (
                id, story_title, story_event, story_note, page_date,
                page_title, page_note, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                story_title = excluded.story_title,
                story_event = excluded.story_event,
                story_note = excluded.story_note,
                page_date = excluded.page_date,
                page_title = excluded.page_title,
                page_note = excluded.page_note,
                updated_at = excluded.updated_at
            """,
            (
                page_id,
                values.get("story_title", ""),
                values.get("story_event", ""),
                values.get("story_note", ""),
                values.get("page_date", ""),
                values.get("page_title", ""),
                values.get("page_note", ""),
                values.get("updated_at"),
            ),
        )


def list_cloud_scrapbook_pages(current_db):
    rows = []
    try:
        for doc in current_db.collection("scrapbook_pages").stream(
            retry=None,
            timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
        ):
            rows.append({"id": doc.id, **doc.to_dict()})
    except Exception as exc:
        print(f"SCRAPBOOK PAGES LOAD ERROR: {exc}")
    return merge_scrapbook_pages(rows)


def upload_to_local(file_storage, section="gallery"):
    os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)
    section = normalize_photo_section(section)
    original_name = secure_filename(file_storage.filename or "photo")
    extension = ""
    if "." in original_name:
        extension = "." + original_name.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}{extension}"
    destination = os.path.join(LOCAL_UPLOAD_DIR, filename)
    file_storage.save(destination)
    return {
        "url": f"/static/uploads/{filename}",
        "storage_path": filename,
        "public_id": filename,
    }


def delete_local_asset(storage_path):
    if not storage_path:
        return
    filename = os.path.basename(str(storage_path).split("?", 1)[0])
    if not filename:
        return
    target = os.path.join(LOCAL_UPLOAD_DIR, filename)
    if os.path.exists(target):
        os.remove(target)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_file_size_bytes(file_storage):
    try:
        stream = file_storage.stream
        current_pos = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(current_pos)
        return size
    except Exception:
        return 0


@app.route("/")
def index():
    local_backend = use_local_backend()
    current_db = None if local_backend else init_firebase()
    current_storage_ready = True if local_backend else storage_ready()
    warnings = []
    featured, gallery, scrapbook = [], [], []
    scrapbook_pages = scrapbook_page_defaults()
    scrapbook_edit_mode = scrapbook_edit_allowed()
    if not local_backend and current_db is None:
        warnings.append(firebase_init_error or "Firebase is not configured.")
    if not local_backend and not current_storage_ready:
        warnings.append(cloudinary_init_error or "Cloudinary is not configured.")

    try:
        if local_backend:
            featured = list_local_photos("featured")[:3]
            gallery = list_local_photos("gallery")
            scrapbook = list_local_photos("scrapbook")
            scrapbook_pages = list_local_scrapbook_pages()
        else:
            if current_db is None:
                raise RuntimeError(firebase_init_error or "Firestore is not configured.")

            featured_ref = current_db.collection("photos").where(
                filter=FieldFilter("section", "==", "featured")
            ).limit(3)
            gallery_ref = current_db.collection("photos").where(
                filter=FieldFilter("section", "==", "gallery")
            )
            scrapbook_ref = current_db.collection("photos").where(
                filter=FieldFilter("section", "==", "scrapbook")
            )

            featured = sorted(
                [
                    {"id": d.id, **d.to_dict()}
                    for d in featured_ref.stream(
                        retry=None,
                        timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
                    )
                ],
                key=lambda x: x.get("uploaded_at") or "",
                reverse=True
            )[:3]

            gallery = sorted(
                [
                    {"id": d.id, **d.to_dict()}
                    for d in gallery_ref.stream(
                        retry=None,
                        timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
                    )
                ],
                key=lambda x: x.get("uploaded_at") or "",
                reverse=True
            )
            scrapbook = sorted(
                [
                    {"id": d.id, **d.to_dict()}
                    for d in scrapbook_ref.stream(
                        retry=None,
                        timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
                    )
                ],
                key=lambda x: x.get("uploaded_at") or "",
                reverse=True
            )
            scrapbook_pages = list_cloud_scrapbook_pages(current_db)
    except Exception as e:
        print(f"INDEX ERROR: {e}")

    total_photos = len(featured) + len(gallery) + len(scrapbook)
    latest_caption = next(
        (
            photo.get("caption", "").strip()
            for photo in (gallery + featured + scrapbook)
            if photo.get("caption", "").strip()
        ),
        ""
    )

    template_file = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")
    )
    try:
        template_mtime = datetime.fromtimestamp(
            os.path.getmtime(template_file), timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        template_mtime = "unknown"

    commit_sha = (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "local")[:7]
    template_version = f"{commit_sha} | {template_mtime}"

    return render_template(
        "index.html",
        featured=featured,
        gallery=gallery,
        scrapbook=scrapbook,
        scrapbook_pages_config=scrapbook_pages,
        scrapbook_edit_mode=scrapbook_edit_mode,
        scrapbook_edit_value=scrapbook_edit_value(),
        scrapbook_edit_is_public=not SCRAPBOOK_EDIT_KEY,
        template_version=template_version,
        firebase_ready=current_db is not None,
        firebase_error=firebase_init_error,
        cloud_storage_ready=current_storage_ready,
        storage_error=cloudinary_init_error,
        warnings=warnings,
        running_on_vercel=RUNNING_ON_VERCEL,
        upload_storage_label=(
            "this computer" if local_backend
            else "Cloudinary" if current_storage_ready
            else "cloud storage not configured"
        ),
        total_photos=total_photos,
        latest_caption=latest_caption,
        max_file_size_mb=MAX_FILE_SIZE_MB,
        max_request_size_mb=MAX_REQUEST_SIZE_MB
    )


@app.route("/favicon.svg")
def favicon():
    # Vercel serves public/** at the root path; this keeps local Flask runs working too.
    public_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "public")
    )
    return send_from_directory(public_dir, "favicon.svg")


@app.route("/uploads/<path:storage_path>")
def uploaded_file(storage_path):
    return "Legacy Firebase Storage uploads are no longer used. New photos are served by Cloudinary.", 404


@app.after_request
def add_no_cache_headers(response):
    # Avoid stale HTML in browser/proxy cache while iterating on template content.
    if response.content_type and response.content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return redirect(url_for("index"))

    is_async_upload = request.headers.get("X-Upload-Async") == "1"
    try:
        local_backend = use_local_backend()
        current_db = None if local_backend else init_firebase()
        if not local_backend and current_db is None:
            if is_async_upload:
                return Response(
                    friendly_upload_error(firebase_init_error or "Firebase is not configured."),
                    status=503,
                )
            flash("Firebase is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))
        if not local_backend and not storage_ready():
            if is_async_upload:
                return Response(
                    friendly_upload_error(cloudinary_init_error or "Cloudinary is not configured."),
                    status=503,
                )
            flash("Cloudinary is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))

        files = [f for f in request.files.getlist("photo") if f and f.filename]
        if not files:
            if is_async_upload:
                return Response("No file selected.", status=400)
            flash("No file selected.", "error")
            return redirect(url_for("index"))

        caption = request.form.get("caption", "").strip()
        section = normalize_photo_section(request.form.get("section", "gallery"))
        uploaded_count = 0
        invalid_count = 0
        too_large_count = 0
        failed_count = 0
        failure_messages = []

        for file in files:
            if not allowed_file(file.filename):
                invalid_count += 1
                continue

            file_size = get_file_size_bytes(file)
            if file_size > MAX_FILE_SIZE_BYTES:
                too_large_count += 1
                continue

            result = None
            try:
                result = (
                    upload_to_local(file, section)
                    if local_backend
                    else upload_to_cloudinary(file, section)
                )

                photo_payload = {
                    "id": uuid.uuid4().hex,
                    "url": result["url"],
                    "storage_path": result["storage_path"],
                    "public_id": result.get("public_id") or result["storage_path"],
                    "caption": caption,
                    "section": section,
                    "uploaded_at": utc_now_iso(),
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                }
                if local_backend:
                    create_local_photo(photo_payload)
                else:
                    current_db.collection("photos").add(
                        {
                            "url": result["url"],
                            "storage_path": result["storage_path"],
                            "public_id": result.get("public_id") or result["storage_path"],
                            "caption": caption,
                            "section": section,
                            "uploaded_at": firestore.SERVER_TIMESTAMP,
                        },
                        retry=None,
                        timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
                    )
                uploaded_count += 1
            except Exception as upload_err:
                if result:
                    if local_backend:
                        delete_local_asset(result.get("storage_path"))
                    else:
                        delete_cloudinary_asset(result.get("public_id") or result.get("storage_path"))
                friendly_error = friendly_upload_error(upload_err)
                failure_messages.append(friendly_error)
                print(f"SINGLE UPLOAD ERROR: {upload_err}")
                failed_count += 1

        if is_async_upload:
            if uploaded_count and not (invalid_count or too_large_count or failed_count):
                return Response(status=204)
            if too_large_count:
                return Response(
                    f"Photo is still too large after compression. Max size is {MAX_FILE_SIZE_MB}MB.",
                    status=413,
                )
            if invalid_count:
                return Response(
                    "Invalid file type. Please upload JPG, PNG, WEBP, or GIF.",
                    status=400,
                )
            if failed_count:
                return Response(
                    failure_messages[0] if failure_messages else "Upload failed.",
                    status=500,
                )
            return Response("No file selected.", status=400)

        if uploaded_count:
            flash(f"{uploaded_count} photo(s) uploaded!", "success")
        if invalid_count:
            flash(f"{invalid_count} file(s) skipped due to invalid file type.", "error")
        if too_large_count:
            flash(
                f"{too_large_count} file(s) skipped: each file must be {MAX_FILE_SIZE_MB}MB or less.",
                "error"
            )
        if failed_count:
            flash(f"{failed_count} file(s) failed to upload. Please try again.", "error")
        if not (uploaded_count or invalid_count or too_large_count or failed_count):
            flash("No file selected.", "error")

    except RequestEntityTooLarge:
        if is_async_upload:
            return Response(
                f"Upload payload too large. Maximum total size per upload is {MAX_REQUEST_SIZE_MB}MB.",
                status=413,
            )
        flash(
            f"Upload payload too large. Maximum total size per upload is {MAX_REQUEST_SIZE_MB}MB.",
            "error"
        )
    except Exception as e:
        print(f"UPLOAD ERROR: {e}")
        traceback.print_exc()
        if is_async_upload:
            return Response(friendly_upload_error(e), status=500)
        flash(f"Upload failed: {str(e)}", "error")

    return redirect(url_for("index"))


@app.route("/scrapbook/page/<page_id>", methods=["POST"])
def update_scrapbook_page(page_id):
    if not scrapbook_edit_allowed():
        flash("Use your private edit link to change the scrapbook.", "error")
        return redirect(url_for("index", _anchor="story"))

    if page_id not in SCRAPBOOK_PAGE_IDS:
        flash("Scrapbook page not found.", "error")
        return redirect(url_for("index", edit=scrapbook_edit_value(), _anchor="story"))

    values = scrapbook_page_form_values(request.form)
    local_backend = use_local_backend()

    try:
        if local_backend:
            update_local_scrapbook_page(page_id, values)
        else:
            current_db = init_firebase()
            if current_db is None:
                raise RuntimeError(firebase_init_error or "Firestore is not configured.")
            current_db.collection("scrapbook_pages").document(page_id).set(
                {
                    **values,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
                retry=None,
                timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
            )
        flash("Scrapbook page saved.", "success")
    except Exception as exc:
        print(f"SCRAPBOOK PAGE UPDATE ERROR: {exc}")
        traceback.print_exc()
        flash("Scrapbook page could not be saved.", "error")

    return redirect(url_for("index", edit=scrapbook_edit_value(), scrapbook="1", _anchor="story"))


@app.route("/replace/<photo_id>", methods=["POST"])
def replace(photo_id):
    result = None
    local_backend = use_local_backend()
    try:
        current_db = None if local_backend else init_firebase()
        if not local_backend and current_db is None:
            flash("Firebase is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))
        if not local_backend and not storage_ready():
            flash("Cloudinary is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))

        if "photo" not in request.files:
            flash("No file selected for replacement.", "error")
            return redirect(url_for("index"))

        file = request.files["photo"]
        if file.filename == "":
            flash("No file selected for replacement.", "error")
            return redirect(url_for("index"))

        if not allowed_file(file.filename):
            flash("Invalid file type.", "error")
            return redirect(url_for("index"))
        if get_file_size_bytes(file) > MAX_FILE_SIZE_BYTES:
            flash(f"Replacement failed: file must be {MAX_FILE_SIZE_MB}MB or less.", "error")
            return redirect(url_for("index"))

        if local_backend:
            data = get_local_photo(photo_id)
        else:
            doc_ref = current_db.collection("photos").document(photo_id)
            doc = doc_ref.get(
                retry=None,
                timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
            )
            data = doc.to_dict() if doc.exists else None
        if not data:
            flash("Featured photo not found.", "error")
            return redirect(url_for("index"))

        section = (
            data.get("section")
            if data.get("section") in {"gallery", "featured", "scrapbook"}
            else "gallery"
        )

        # Upload the new image first so we do not lose the existing one if upload fails.
        result = (
            upload_to_local(file, section)
            if local_backend
            else upload_to_cloudinary(file, section)
        )

        if local_backend:
            update_local_photo(
                photo_id,
                {
                    "url": result["url"],
                    "storage_path": result["storage_path"],
                    "public_id": result.get("public_id") or result["storage_path"],
                    "updated_at": utc_now_iso(),
                    "uploaded_at": utc_now_iso(),
                },
            )
        else:
            doc_ref.update(
                {
                    "url": result["url"],
                    "storage_path": result["storage_path"],
                    "public_id": result.get("public_id") or result["storage_path"],
                    "uploaded_at": firestore.SERVER_TIMESTAMP,
                },
                retry=None,
                timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
            )
        old_storage_path = data.get("public_id") or data.get("storage_path")
        if local_backend:
            delete_local_asset(old_storage_path)
        else:
            delete_cloudinary_asset(old_storage_path)

        flash("Featured photo replaced.", "success")
    except Exception as e:
        if result:
            if local_backend:
                delete_local_asset(result.get("storage_path"))
            else:
                delete_cloudinary_asset(result.get("public_id") or result.get("storage_path"))
        print(f"REPLACE ERROR: {e}")
        traceback.print_exc()
        flash(f"Replace failed: {str(e)}", "error")

    return redirect(url_for("index"))


@app.route("/delete/<photo_id>", methods=["POST"])
def delete(photo_id):
    try:
        local_backend = use_local_backend()
        current_db = None if local_backend else init_firebase()
        if not local_backend and current_db is None:
            flash("Firebase is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))

        if local_backend:
            data = get_local_photo(photo_id)
            if data:
                delete_local_asset(data.get("storage_path") or data.get("public_id"))
                delete_local_photo(photo_id)
                flash("Photo deleted.", "success")
        else:
            doc = current_db.collection("photos").document(photo_id).get(
                retry=None,
                timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS
            )
            if not doc.exists:
                return redirect(url_for("index"))
            data = doc.to_dict()
            delete_cloudinary_asset(data.get("public_id") or data.get("storage_path"))
            current_db.collection("photos").document(photo_id).delete(
                retry=None,
                timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS
            )
            flash("Photo deleted.", "success")
    except Exception as e:
        print(f"DELETE ERROR: {e}")
        flash("Delete failed.", "error")

    return redirect(url_for("index"))


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_error):
    if request.headers.get("X-Upload-Async") == "1":
        return Response(
            f"Upload payload too large. Maximum total size per upload is {MAX_REQUEST_SIZE_MB}MB.",
            status=413,
        )
    flash(
        f"Upload payload too large. Maximum total size per upload is {MAX_REQUEST_SIZE_MB}MB.",
        "error"
    )
    return redirect(url_for("index"))


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException) and error.code != 500:
        return error

    print(f"UNEXPECTED ERROR: {error}")
    traceback.print_exc()
    if request.headers.get("X-Upload-Async") == "1":
        return Response("Upload failed.", status=500)
    flash("Something went wrong. Try a smaller photo or check the deployment settings.", "error")
    return redirect(url_for("index"))
