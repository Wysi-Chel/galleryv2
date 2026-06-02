import os
import json
import sqlite3
import uuid
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
import firebase_admin
from firebase_admin import credentials, firestore, storage
from google.cloud.firestore_v1 import FieldFilter

# ── Flask ──
app = Flask(
    __name__,
    template_folder="../templates",
    static_folder="../static",
    static_url_path="/static"
)
app.secret_key = os.environ.get("SECRET_KEY", "*qaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqaqa")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "25"))
MAX_REQUEST_SIZE_MB = int(os.environ.get("MAX_REQUEST_SIZE_MB", "100"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
FIREBASE_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("FIREBASE_REQUEST_TIMEOUT_SECONDS", "10"))
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_SIZE_MB * 1024 * 1024


APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOCAL_DB_PATH = os.environ.get("SQLITE_PATH") or os.path.join(APP_ROOT, "data", "memory_house.db")
LOCAL_UPLOAD_DIR = os.environ.get("LOCAL_UPLOAD_DIR") or os.path.join(APP_ROOT, "static", "uploads")
db = None
firebase_init_error = None
storage_bucket = None
storage_init_error = None
RUNNING_ON_VERCEL = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))
FIREBASE_STORAGE_BUCKET = (
    os.environ.get("FIREBASE_STORAGE_BUCKET")
    or os.environ.get("GOOGLE_CLOUD_STORAGE_BUCKET")
    or ""
).replace("gs://", "").strip().strip("/")


def use_local_backend():
    if RUNNING_ON_VERCEL:
        return False
    database_backend = os.environ.get("DATABASE_BACKEND", "").strip().lower()
    storage_backend = os.environ.get("STORAGE_BACKEND", "").strip().lower()
    return (
        database_backend == "sqlite"
        or storage_backend == "local"
        or not FIREBASE_STORAGE_BUCKET
    )


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_photo_section(section):
    return section if section in {"gallery", "featured", "scrapbook"} else "gallery"


# Firebase handles both Firestore metadata and durable image storage.
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
            if FIREBASE_STORAGE_BUCKET:
                firebase_admin.initialize_app(
                    cred,
                    {"storageBucket": FIREBASE_STORAGE_BUCKET},
                )
            else:
                firebase_admin.initialize_app(cred)

        db = firestore.client()
        firebase_init_error = None
    except Exception as exc:
        firebase_init_error = str(exc)
        db = None
        print(f"FIREBASE INIT ERROR: {firebase_init_error}")

    return db


def init_storage_bucket():
    global storage_bucket, storage_init_error

    if storage_bucket is not None:
        return storage_bucket

    if init_firebase() is None:
        storage_init_error = firebase_init_error or "Firebase is not configured."
        return None
    if not FIREBASE_STORAGE_BUCKET:
        storage_init_error = (
            "Missing FIREBASE_STORAGE_BUCKET. Use the bucket name from Firebase Storage "
            "without gs://, for example your-project.firebasestorage.app."
        )
        return None

    try:
        storage_bucket = storage.bucket(FIREBASE_STORAGE_BUCKET)
        storage_init_error = None
    except Exception as exc:
        storage_init_error = str(exc)
        storage_bucket = None
        print(f"FIREBASE STORAGE INIT ERROR: {storage_init_error}")

    return storage_bucket


def storage_ready():
    return init_storage_bucket() is not None


def build_storage_url(storage_path):
    return f"/uploads/{quote(storage_path, safe='/')}"


def upload_to_storage(file_storage, section="gallery"):
    bucket = init_storage_bucket()
    if bucket is None:
        raise RuntimeError(storage_init_error or "Firebase Storage is not configured.")

    section = section if section in {"gallery", "featured", "scrapbook"} else "gallery"
    original_name = secure_filename(file_storage.filename or "photo")
    extension = ""
    if "." in original_name:
        extension = "." + original_name.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}{extension}"
    storage_path = f"couple_gallery/{section}/{filename}"
    blob = bucket.blob(storage_path)
    content_type = file_storage.mimetype or "application/octet-stream"

    file_storage.stream.seek(0)
    blob.upload_from_file(file_storage.stream, content_type=content_type)

    return {
        "url": build_storage_url(storage_path),
        "storage_path": storage_path,
    }


def delete_storage_object(storage_path):
    if not storage_path:
        return
    bucket = init_storage_bucket()
    if bucket is None:
        print(f"FIREBASE STORAGE DELETE SKIPPED: {storage_init_error}")
        return
    try:
        bucket.blob(storage_path).delete()
    except Exception as exc:
        # Keep Firestore cleanup successful even if a stale object is already gone.
        print(f"FIREBASE STORAGE DELETE WARNING: {exc}")


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
            return

        create_sql = table["sql"] or ""
        if "CHECK" in create_sql.upper() and "SCRAPBOOK" not in create_sql.upper():
            migrate_local_photos_table(conn)
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
    if not local_backend and current_db is None:
        warnings.append(firebase_init_error or "Firebase is not configured.")
    if not local_backend and not current_storage_ready:
        warnings.append(storage_init_error or "Firebase Storage is not configured.")

    try:
        if local_backend:
            featured = list_local_photos("featured")[:3]
            gallery = list_local_photos("gallery")
            scrapbook = list_local_photos("scrapbook")
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
        template_version=template_version,
        firebase_ready=current_db is not None,
        firebase_error=firebase_init_error,
        cloud_storage_ready=current_storage_ready,
        storage_error=storage_init_error,
        warnings=warnings,
        running_on_vercel=RUNNING_ON_VERCEL,
        upload_storage_label=(
            "this computer" if local_backend
            else "Firebase Storage" if current_storage_ready
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
    bucket = init_storage_bucket()
    if bucket is None:
        return "Firebase Storage is not configured.", 404

    storage_path = storage_path.strip().lstrip("/")
    if not storage_path.startswith("couple_gallery/"):
        return "File not found.", 404

    blob = bucket.blob(storage_path)
    try:
        signed_url = blob.generate_signed_url(
            expiration=timedelta(minutes=15),
            method="GET",
        )
    except Exception as exc:
        print(f"FIREBASE STORAGE SIGNED URL ERROR: {exc}")
        return "File could not be loaded.", 404

    return redirect(signed_url)


@app.after_request
def add_no_cache_headers(response):
    # Avoid stale HTML in browser/proxy cache while iterating on template content.
    if response.content_type and response.content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.route("/upload", methods=["POST"])
def upload():
    try:
        local_backend = use_local_backend()
        current_db = None if local_backend else init_firebase()
        if not local_backend and current_db is None:
            flash("Firebase is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))
        if not local_backend and not storage_ready():
            flash("Firebase Storage is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))

        files = [f for f in request.files.getlist("photo") if f and f.filename]
        if not files:
            flash("No file selected.", "error")
            return redirect(url_for("index"))

        caption = request.form.get("caption", "").strip()
        section = normalize_photo_section(request.form.get("section", "gallery"))
        uploaded_count = 0
        invalid_count = 0
        too_large_count = 0
        failed_count = 0

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
                    else upload_to_storage(file, section)
                )

                photo_payload = {
                    "id": uuid.uuid4().hex,
                    "url": result["url"],
                    "storage_path": result["storage_path"],
                    "public_id": result["storage_path"],
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
                            "public_id": result["storage_path"],
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
                        delete_storage_object(result.get("storage_path"))
                print(f"SINGLE UPLOAD ERROR: {upload_err}")
                failed_count += 1

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

    except Exception as e:
        print(f"UPLOAD ERROR: {e}")
        traceback.print_exc()
        flash(f"Upload failed: {str(e)}", "error")

    return redirect(url_for("index"))


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
            flash("Firebase Storage is not configured on this deployment yet.", "error")
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
            else upload_to_storage(file, section)
        )

        if local_backend:
            update_local_photo(
                photo_id,
                {
                    "url": result["url"],
                    "storage_path": result["storage_path"],
                    "public_id": result["storage_path"],
                    "updated_at": utc_now_iso(),
                    "uploaded_at": utc_now_iso(),
                },
            )
        else:
            doc_ref.update(
                {
                    "url": result["url"],
                    "storage_path": result["storage_path"],
                    "public_id": result["storage_path"],
                    "uploaded_at": firestore.SERVER_TIMESTAMP,
                },
                retry=None,
                timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
            )
        old_storage_path = data.get("storage_path") or data.get("public_id")
        if local_backend:
            delete_local_asset(old_storage_path)
        else:
            delete_storage_object(old_storage_path)

        flash("Featured photo replaced.", "success")
    except Exception as e:
        if result:
            if local_backend:
                delete_local_asset(result.get("storage_path"))
            else:
                delete_storage_object(result.get("storage_path"))
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
            delete_storage_object(data.get("storage_path") or data.get("public_id"))
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
    flash(
        f"Upload payload too large. Maximum total size per upload is {MAX_REQUEST_SIZE_MB}MB.",
        "error"
    )
    return redirect(url_for("index"))
