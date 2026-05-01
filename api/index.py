import os
import json
import uuid
import traceback
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
import firebase_admin
from firebase_admin import credentials, firestore
import cloudinary
import cloudinary.uploader

# ── Flask ──
app = Flask(
    __name__,
    template_folder="../templates",
    static_folder="../static",
    static_url_path="/static"
)
app.secret_key = os.environ.get("SECRET_KEY", "chelianna")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "25"))
MAX_REQUEST_SIZE_MB = int(os.environ.get("MAX_REQUEST_SIZE_MB", "100"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_SIZE_MB * 1024 * 1024

# ── Cloudinary Init ──
CLOUDINARY_ENV_KEYS = {
    "cloud_name": "CLOUDINARY_CLOUD_NAME",
    "api_key": "CLOUDINARY_API_KEY",
    "api_secret": "CLOUDINARY_API_SECRET"
}

db = None
firebase_init_error = None


def load_cloudinary_config():
    config = {
        "cloud_name": os.environ.get(CLOUDINARY_ENV_KEYS["cloud_name"]),
        "api_key": os.environ.get(CLOUDINARY_ENV_KEYS["api_key"]),
        "api_secret": os.environ.get(CLOUDINARY_ENV_KEYS["api_secret"])
    }

    if all(config.values()):
        return config

    config_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "cloudinary.txt")
    )
    if not os.path.exists(config_path):
        return config

    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            lines = [line.strip() for line in fh.readlines() if line.strip()]

        for idx, line in enumerate(lines):
            lower = line.lower()
            if lower == "cloud name" and idx + 1 < len(lines):
                config["cloud_name"] = config["cloud_name"] or lines[idx + 1]
            elif lower.startswith("api key"):
                parts = line.split(" ", 2)
                if len(parts) >= 3:
                    config["api_key"] = config["api_key"] or parts[2]
            elif lower.startswith("api secret"):
                parts = line.split(" ", 2)
                if len(parts) >= 3:
                    config["api_secret"] = config["api_secret"] or parts[2]
    except Exception as exc:
        print(f"CLOUDINARY CONFIG READ ERROR: {exc}")

    return config


cloudinary_settings = load_cloudinary_config()
cloudinary.config(
    cloud_name=cloudinary_settings.get("cloud_name"),
    api_key=cloudinary_settings.get("api_key"),
    api_secret=cloudinary_settings.get("api_secret"),
    secure=True
)


def cloudinary_ready():
    return all([
        cloudinary_settings.get("cloud_name"),
        cloudinary_settings.get("api_key"),
        cloudinary_settings.get("api_secret")
    ])


# ── Firebase Init (Firestore only, no Storage) ──
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
    try:
        if current_db is None:
            raise RuntimeError(firebase_init_error or "Firestore is not configured.")

        featured_ref = current_db.collection("photos").where("section", "==", "featured").limit(3)
        gallery_ref  = current_db.collection("photos").where("section", "==", "gallery")

        featured = sorted(
            [{"id": d.id, **d.to_dict()} for d in featured_ref.stream()],
            key=lambda x: x.get("uploaded_at") or "",
            reverse=True
        )[:3]

        gallery = sorted(
            [{"id": d.id, **d.to_dict()} for d in gallery_ref.stream()],
            key=lambda x: x.get("uploaded_at") or "",
            reverse=True
        )
    except Exception as e:
        print(f"INDEX ERROR: {e}")
        featured, gallery = [], []

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
        template_version=template_version,
        firebase_ready=current_db is not None,
        firebase_error=firebase_init_error,
        cloudinary_ready=cloudinary_ready()
    )


@app.route("/favicon.svg")
def favicon():
    # Vercel serves public/** at the root path; this keeps local Flask runs working too.
    public_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "public")
    )
    return send_from_directory(public_dir, "favicon.svg")


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
        if not cloudinary_ready():
            flash("Cloudinary is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))

        files = [f for f in request.files.getlist("photo") if f and f.filename]
        if not files:
            flash("No file selected.", "error")
            return redirect(url_for("index"))

        caption = request.form.get("caption", "").strip()
        section = request.form.get("section", "gallery")
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

            try:
                result = cloudinary.uploader.upload(
                    file,
                    folder="couple_gallery",
                    public_id=uuid.uuid4().hex,
                    overwrite=False,
                    resource_type="image"
                )

                current_db.collection("photos").add({
                    "url": result["secure_url"],
                    "public_id": result["public_id"],
                    "caption": caption,
                    "section": section,
                    "uploaded_at": firestore.SERVER_TIMESTAMP
                })
                uploaded_count += 1
            except Exception as upload_err:
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
    try:
        current_db = init_firebase()
        if current_db is None:
            flash("Firebase is not configured on this deployment yet.", "error")
            return redirect(url_for("index"))
        if not cloudinary_ready():
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

        doc_ref = current_db.collection("photos").document(photo_id)
        doc = doc_ref.get()
        if not doc.exists:
            flash("Featured photo not found.", "error")
            return redirect(url_for("index"))

        data = doc.to_dict() or {}

        # Upload the new image first so we do not lose the existing one if upload fails
        result = cloudinary.uploader.upload(
            file,
            folder="couple_gallery",
            public_id=uuid.uuid4().hex,
            overwrite=False,
            resource_type="image"
        )

        old_public_id = data.get("public_id")
        if old_public_id:
            try:
                cloudinary.uploader.destroy(old_public_id)
            except Exception as destroy_err:
                # Keep replacement successful even if old asset cleanup fails
                print(f"CLOUDINARY DESTROY WARNING: {destroy_err}")

        doc_ref.update({
            "url": result["secure_url"],
            "public_id": result["public_id"],
            "uploaded_at": firestore.SERVER_TIMESTAMP
        })

        flash("Featured photo replaced.", "success")
    except Exception as e:
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

        doc = current_db.collection("photos").document(photo_id).get()
        if doc.exists:
            data = doc.to_dict()
            # Delete from Cloudinary
            if data.get("public_id"):
                cloudinary.uploader.destroy(data["public_id"])
            # Delete from Firestore
            current_db.collection("photos").document(photo_id).delete()
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
