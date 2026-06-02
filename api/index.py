import os
import json
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
    current_db = init_firebase()
    current_storage_ready = storage_ready()
    warnings = []
    featured, gallery, scrapbook = [], [], []
    if current_db is None:
        warnings.append(firebase_init_error or "Firebase is not configured.")
    if not current_storage_ready:
        warnings.append(storage_init_error or "Firebase Storage is not configured.")

    try:
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
            "Firebase Storage" if current_storage_ready else "cloud storage not configured"
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
        current_db = init_firebase()
        if current_db is None:
            flash("Firebase is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))
        if not storage_ready():
            flash("Firebase Storage is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))

        files = [f for f in request.files.getlist("photo") if f and f.filename]
        if not files:
            flash("No file selected.", "error")
            return redirect(url_for("index"))

        caption = request.form.get("caption", "").strip()
        section = request.form.get("section", "gallery")
        if section not in {"gallery", "featured", "scrapbook"}:
            section = "gallery"
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
                result = upload_to_storage(file, section)

                current_db.collection("photos").add(
                    {
                        "url": result["url"],
                        "storage_path": result["storage_path"],
                        "public_id": result["storage_path"],
                        "caption": caption,
                        "section": section,
                        "uploaded_at": firestore.SERVER_TIMESTAMP
                    },
                    retry=None,
                    timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
                )
                uploaded_count += 1
            except Exception as upload_err:
                if result:
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
    try:
        current_db = init_firebase()
        if current_db is None:
            flash("Firebase is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))
        if not storage_ready():
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

        doc_ref = current_db.collection("photos").document(photo_id)
        doc = doc_ref.get(
            retry=None,
            timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
        )
        if not doc.exists:
            flash("Featured photo not found.", "error")
            return redirect(url_for("index"))

        data = doc.to_dict() or {}
        section = (
            data.get("section")
            if data.get("section") in {"gallery", "featured", "scrapbook"}
            else "gallery"
        )

        # Upload the new image first so we do not lose the existing one if upload fails.
        result = upload_to_storage(file, section)

        doc_ref.update(
            {
                "url": result["url"],
                "storage_path": result["storage_path"],
                "public_id": result["storage_path"],
                "uploaded_at": firestore.SERVER_TIMESTAMP
            },
            retry=None,
            timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS,
        )
        old_storage_path = data.get("storage_path") or data.get("public_id")
        delete_storage_object(old_storage_path)

        flash("Featured photo replaced.", "success")
    except Exception as e:
        if result:
            delete_storage_object(result.get("storage_path"))
        print(f"REPLACE ERROR: {e}")
        traceback.print_exc()
        flash(f"Replace failed: {str(e)}", "error")

    return redirect(url_for("index"))


@app.route("/delete/<photo_id>", methods=["POST"])
def delete(photo_id):
    try:
        current_db = init_firebase()
        if current_db is None:
            flash("Firebase is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))

        doc = current_db.collection("photos").document(photo_id).get(
            retry=None,
            timeout=FIREBASE_REQUEST_TIMEOUT_SECONDS
        )
        if doc.exists:
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
