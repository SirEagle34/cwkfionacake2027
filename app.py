# Standard library imports
import argparse
import base64
import gzip
import hmac
import hashlib
import json
import logging
import os
import random
import re
import secrets
import smtplib
import shutil
import sqlite3
import string
import subprocess
import threading
import traceback
import time
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import parse_qs
from typing import Optional
from RemoveGameItems import remove_game_items, ENABLED as REMOVE_GAME_ITEMS_ENABLED, DATABASE_TARGET as REMOVE_GAME_ITEMS_DATABASE_TARGET

# Third-party library imports
import schedule

import secret as _secret_module

from secret import (
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_PASSWORD,
    SMTP_FROM
)

from flask import (Flask, Request, abort, jsonify, make_response, redirect,
                   render_template, request, send_from_directory, session)
from flask_bcrypt import Bcrypt
from flask_login import (LoginManager, UserMixin, current_user,
                         login_required, login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from flask_sqlalchemy.session import Session as FlaskSQLAlchemySession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import create_engine
from sqlalchemy.sql import func

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=5000)
parser.add_argument('--debug', action='store_true')

args, _ = parser.parse_known_args()

app = Flask(__name__)

# Route Player ORM operations to LocalInstance when the requested account
# exists there. Other models (admins, logs, bans, store counters) stay on Primary.
_LOCAL_PLAYER_ENGINE = None
_LOCAL_PLAYER_ENGINE_PATH = None

def _requested_player_identity():
    if not request:
        return ""
    identity = (request.headers.get("Player-Id") or "").strip()
    if not identity:
        identity = (request.view_args or {}).get("player", "") or ""
    if not identity:
        identity = request.args.get("player_id", "") or request.args.get("username", "")
    if not identity:
        identity = request.form.get("player_id", "") or request.form.get("username", "")
    if not identity:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            identity = payload.get("player_id", "") or payload.get("username", "")
    return str(identity).strip()[:80]

def _local_instance_db_path():
    manifest_path = os.path.join(app.root_path, "localinstance", "manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        for item in manifest.get("databases", []):
            if isinstance(item, dict) and item.get("id") == "local" and item.get("enabled", True):
                filename = str(item.get("file", "local_001_cardwarskingdom.db"))
                if os.path.basename(filename) == filename and filename.endswith(".db"):
                    return os.path.join(app.root_path, "localinstance", filename)
    except (OSError, ValueError, TypeError):
        return None
    return None

def _local_instance_has_player(username):
    if not username:
        return False
    path = _local_instance_db_path()
    if not path or not os.path.isfile(path):
        return False
    try:
        connection = sqlite3.connect("file:" + os.path.abspath(path) + "?mode=ro", uri=True, timeout=2)
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "player" not in tables:
                return False
            columns = {row[1] for row in connection.execute('PRAGMA table_info("player")')}
            if "username" not in columns:
                return False
            return connection.execute(
                'SELECT 1 FROM "player" WHERE "username" = ? LIMIT 1', (username,)
            ).fetchone() is not None
        finally:
            connection.close()
    except sqlite3.Error:
        app.logger.exception("Could not inspect LocalInstance player identity")
        return False

def _local_instance_engine():
    global _LOCAL_PLAYER_ENGINE, _LOCAL_PLAYER_ENGINE_PATH
    path = _local_instance_db_path()
    if not path or not os.path.isfile(path):
        return None
    if _LOCAL_PLAYER_ENGINE is None or _LOCAL_PLAYER_ENGINE_PATH != path:
        if _LOCAL_PLAYER_ENGINE is not None:
            _LOCAL_PLAYER_ENGINE.dispose()
        _LOCAL_PLAYER_ENGINE = create_engine(
            "sqlite:///" + path.replace("\\", "/"),
            connect_args={"timeout": 15, "check_same_thread": False}
        )
        _LOCAL_PLAYER_ENGINE_PATH = path
    return _LOCAL_PLAYER_ENGINE

class PlayerDatabaseRoutingSession(FlaskSQLAlchemySession):
    def get_bind(self, mapper=None, clause=None, bind=None, **kwargs):
        if mapper is not None and getattr(getattr(mapper, "class_", None), "__name__", "") == "Player":
            try:
                username = _requested_player_identity()
                if username and _local_instance_has_player(username):
                    local_engine = _local_instance_engine()
                    if local_engine is not None:
                        return local_engine
            except RuntimeError:
                # No active Flask request (e.g. scheduled/background task).
                pass
        return super().get_bind(mapper=mapper, clause=clause, bind=bind, **kwargs)


#check if flaskkey exists
if not os.path.exists("flaskkey"):
	print("Creating new Flask secret key")
	#create a new key
	with open("flaskkey", "w") as f:
		f.write(''.join(random.choice(string.ascii_letters + string.digits) for i in range(50)))
app.secret_key = open("flaskkey", "r").read()

bcrypt = Bcrypt(app)

login_manager = LoginManager(app)
login_manager.login_view = '/'


class Base(DeclarativeBase):
  pass
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///cardwarskingdom.db"
db = SQLAlchemy(model_class=Base, session_options={"class_": PlayerDatabaseRoutingSession})
db.init_app(app)

badcharaters = ['/', '\\', ':', '*', '?', '"', '<', '>', '|', ";", "%", "^", "&", "(", ")", "{", "}", "[", "]", ".", ",", "'", "`", "!", "$", "#", "@", "+", "="]

maintenance = False

@app.route("/static/version.txt")
def PersistVersion():
	with open("data/persist/version.txt", "r") as f:
		pc_version = f.read()
	with open("data/persist/android_version.txt", "r") as f:
		android_version = f.read()

	data = {
		"maintenance_mode": "yes" if maintenance else "no",
		"message": "Card Wars Kingdom is currently undergoing maintenance.\n\nPlease try again later.",
		"icon": "",
		"clickable": "yes",
		"android_version": android_version,
		"version": pc_version,
		"android_url": "https://github.com/shishkabob27/CardWarsKingdom/releases",
		"pc_url": "https://github.com/shishkabob27/CardWarsKingdom/releases",
	}
	return json.dumps(data)

class AdminActivity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    time = db.Column(db.Integer, nullable=False, default=int(time.time()))
    message = db.Column(db.String(8192), nullable=True)

def DiscordWebhookMessage(message):

	newActivity = AdminActivity(
		time=int(time.time()),
		message=message
	)
	db.session.add(newActivity)
	db.session.commit()

	#check if the file exists
	if not os.path.exists("discordwebhookurl"):
		return
	else:
		with open("discordwebhookurl", "r") as f:
			url = f.read()
	try:
		webhook = discord_webhook.DiscordWebhook(url=url, content=message)
		webhook.execute()
	except:
		Log("admin", "Failed to send webhook message: " + message)
		pass

class Admin(UserMixin, db.Model):
    username: Mapped[str] = mapped_column(
        db.String(80),
        primary_key=True,
        unique=True,
        nullable=False
    )

    password: Mapped[str] = mapped_column(
        db.String(255),
        nullable=False
    )

    rank: Mapped[int] = mapped_column(
        db.Integer,
        nullable=False
    )

    email: Mapped[Optional[str]] = mapped_column(
        db.String(255),
        unique=True,
        nullable=True
    )

    email_verified: Mapped[bool] = mapped_column(
        db.Boolean,
        nullable=False,
        default=False
    )

    passkey_hash: Mapped[Optional[str]] = mapped_column(
        db.String(255),
        nullable=True
    )

    verification_code_hash: Mapped[Optional[str]] = mapped_column(
        db.String(255),
        nullable=True
    )

    verification_code_expires: Mapped[Optional[int]] = mapped_column(
        db.Integer,
        nullable=True
    )

    def get_id(self):
        return str(self.username)


class BalancerAccount(db.Model):
    """Separate credentials for Balancer panel users."""
    username: Mapped[str] = mapped_column(
        db.String(80), primary_key=True, unique=True, nullable=False
    )
    password: Mapped[str] = mapped_column(db.String(255), nullable=False)
    created_by: Mapped[str] = mapped_column(db.String(80), nullable=False)
    created_at: Mapped[int] = mapped_column(db.Integer, nullable=False, default=lambda: int(time.time()))


def get_balancer_account():
    username = session.get("balancer_username")
    if not username:
        return None
    return BalancerAccount.query.filter_by(username=username).first()


def MigrateAdminSecurity():
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)

    if "admin" not in inspector.get_table_names():
        print("[Admin Migration] Admin table does not exist.")
        return

    existing_columns = {
        column["name"]
        for column in inspector.get_columns("admin")
    }

    migrations = {
        "email": """
            ALTER TABLE admin
            ADD COLUMN email VARCHAR(255)
        """,

        "email_verified": """
            ALTER TABLE admin
            ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT 0
        """,

        "passkey_hash": """
            ALTER TABLE admin
            ADD COLUMN passkey_hash VARCHAR(255)
        """,

        "verification_code_hash": """
            ALTER TABLE admin
            ADD COLUMN verification_code_hash VARCHAR(255)
        """,

        "verification_code_expires": """
            ALTER TABLE admin
            ADD COLUMN verification_code_expires INTEGER
        """
    }

    with db.engine.begin() as connection:

        for column_name, sql in migrations.items():

            if column_name in existing_columns:
                print(
                    f"[Admin Migration] "
                    f"'{column_name}' already exists. Skipping."
                )
                continue

            connection.execute(text(sql))

            print(
                f"[Admin Migration] "
                f"Added '{column_name}'."
            )

        connection.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            ix_admin_email_unique
            ON admin (email)
        """))

    print("[Admin Migration] Complete.")

@login_manager.user_loader
def load_user(user_id):
	return Admin.query.get(user_id)

@app.route("/admin", methods=['GET', 'POST'])
def AdminPage():

    # Create an admin account if one doesn't exist
    if not Admin.query.first():
        randompassword = ''.join(
            random.choices(
                string.ascii_letters + string.digits,
                k=24
            )
        )

        newAdmin = Admin(
            username="admin",
            password=bcrypt.generate_password_hash(
                randompassword
            ).decode('utf-8'),
            rank=0
        )

        db.session.add(newAdmin)
        db.session.commit()

        Log("server", "Created admin account")

        print(
            f"Admin account created! "
            f"Username: admin, Password: {randompassword}"
        )

    if request.method == 'GET':

        if current_user.is_authenticated:

            if not isAdmin(current_user):
                return abort(404)

            return redirect("/admin/home")

        else:
            return render_template('admin_login.html')


    if request.method == 'POST':

        # Get login method
        login_method = request.form.get(
            'login_method',
            'password'
        )


        # =========================================================
        # NORMAL PASSWORD LOGIN
        # =========================================================

        if login_method == 'password':

            username = request.form.get(
                'username',
                ''
            )

            password = request.form.get(
                'password',
                ''
            )

            username = re.sub(
                r'[^a-zA-Z0-9]',
                '',
                username
            )

            db_user = Admin.query.filter_by(
                username=username
            ).first()

            if db_user is None:
                return make_response(
                    "Invalid Username or Password!",
                    400
                )

            if not bcrypt.check_password_hash(
                db_user.password,
                password
            ):
                return make_response(
                    "Invalid Password or Username!",
                    400
                )


            # =========================================================
            # EMAIL LOGIN VERIFICATION
            # =========================================================

            # If email is verified, require 6-digit OTP
            if db_user.email_verified:

                # Make sure an email address exists
                if not db_user.email:

                    return make_response(
                        "Email verification is enabled, but no email address is configured for this account!",
                        400
                    )

                # Generate 6-digit OTP
                otp = f"{secrets.randbelow(1000000):06d}"

                # Hash OTP
                otp_hash = hashlib.sha256(
                    otp.encode('utf-8')
                ).hexdigest()

                # Store login OTP in session
                session['admin_login_otp_hash'] = otp_hash

                session['admin_login_user'] = db_user.username

                session['admin_login_otp_expires'] = (
                    datetime.utcnow() + timedelta(minutes=5)
                ).timestamp()

                try:

                    send_email_login_otp(
                        db_user.email,
                        otp
                    )

                except Exception as e:

                    # Clear login OTP session
                    session.pop(
                        'admin_login_otp_hash',
                        None
                    )

                    session.pop(
                        'admin_login_user',
                        None
                    )

                    session.pop(
                        'admin_login_otp_expires',
                        None
                    )

                    print(
                        "[LOGIN EMAIL ERROR]",
                        repr(e)
                    )

                    Log(
                        "admin",
                        "Failed to send admin login OTP: " + repr(e)
                    )

                    return make_response(
                        "Failed to send login verification email: " + str(e),
                        500
                    )

                Log(
                    "admin",
                    "Login verification code sent to admin email"
                )

                return redirect("/admin/verify-login")


            # =========================================================
            # EMAIL IS NOT VERIFIED
            # =========================================================

            login_user(
                db_user,
                remember=True
            )

            return redirect("/admin")


        # =========================================================
        # PASS-KEY LOGIN
        # =========================================================

        elif login_method == 'passkey':

            passkey = request.form.get(
                'passkey',
                ''
            ).strip()

            if not passkey:
                return make_response(
                    "Pass-Key is required!",
                    400
                )

            # The Pass-Key is only for the main admin account
            db_user = Admin.query.filter_by(
                username="admin"
            ).first()

            if db_user is None:
                return make_response(
                    "Admin account not found!",
                    404
                )

            # Make sure a Pass-Key has been generated
            if not db_user.passkey_hash:
                return make_response(
                    "Pass-Key has not been configured yet!",
                    400
                )

            # Check Pass-Key hash
            if not bcrypt.check_password_hash(
                db_user.passkey_hash,
                passkey
            ):
                return make_response(
                    "Invalid Pass-Key!",
                    400
                )

            # Pass-Key authentication successful
            login_user(
                db_user,
                remember=True
            )

            Log(
                "admin",
                "Admin logged in using Pass-Key"
            )

            return redirect("/admin")


        # =========================================================
        # INVALID LOGIN METHOD
        # =========================================================

        else:

            return make_response(
                "Invalid login method!",
                400
            )

def isAdmin(user):
	if not user.is_authenticated:
		return False
	db_user = Admin.query.filter_by(username=user.username).first()
	return db_user is not None

def send_email_change_otp(to_email, otp):
    msg = EmailMessage()

    msg["Subject"] = "Admin Email Change Verification Code"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email

    msg.set_content(
        f"""Your email change verification code is:

{otp}

This code will expire in 5 minutes.

If you did not request this change, you can safely ignore this email.
"""
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()

        server.login(
            SMTP_USERNAME,
            SMTP_PASSWORD
        )

        server.send_message(msg)

def send_email_login_otp(to_email, otp):
    msg = EmailMessage()

    msg["Subject"] = "Admin Login Verification Code"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email

    msg.set_content(
        f"""Your administrator login verification code is:

{otp}

This code will expire in 5 minutes.

If you did not attempt to login, please secure your account immediately.
"""
    )

    with smtplib.SMTP(
        SMTP_HOST,
        SMTP_PORT
    ) as server:

        server.starttls()

        server.login(
            SMTP_USERNAME,
            SMTP_PASSWORD
        )

        server.send_message(msg)

@app.route("/admin/verify-login", methods=['GET', 'POST'])
def AdminVerifyLogin():

    # =========================================================
    # GET
    # =========================================================

    if request.method == 'GET':

        if not session.get(
            'admin_login_otp_hash'
        ):

            return redirect("/admin")

        return render_template(
            'admin_login_otp.html'
        )


    # =========================================================
    # POST
    # =========================================================

    otp = request.form.get(
        'otp',
        ''
    ).strip()

    stored_hash = session.get(
        'admin_login_otp_hash'
    )

    username = session.get(
        'admin_login_user'
    )

    expires_at = session.get(
        'admin_login_otp_expires'
    )


    # =========================================================
    # CHECK OTP SESSION
    # =========================================================

    if not stored_hash or not username or not expires_at:

        return render_template(
            'admin_login_otp.html',
            error="No active login verification request found."
        )


    # =========================================================
    # CHECK EXPIRATION
    # =========================================================

    if datetime.utcnow().timestamp() > float(
        expires_at
    ):

        session.pop(
            'admin_login_otp_hash',
            None
        )

        session.pop(
            'admin_login_user',
            None
        )

        session.pop(
            'admin_login_otp_expires',
            None
        )

        return render_template(
            'admin_login_otp.html',
            error="The verification code has expired. Please login again."
        )


    # =========================================================
    # CHECK OTP FORMAT
    # =========================================================

    if len(otp) != 6 or not otp.isdigit():

        return render_template(
            'admin_login_otp.html',
            error="Verification code must be 6 digits."
        )


    # =========================================================
    # HASH ENTERED OTP
    # =========================================================

    entered_hash = hashlib.sha256(
        otp.encode('utf-8')
    ).hexdigest()


    # =========================================================
    # COMPARE OTP
    # =========================================================

    if not secrets.compare_digest(
        entered_hash,
        stored_hash
    ):

        return render_template(
            'admin_login_otp.html',
            error="Invalid verification code."
        )


    # =========================================================
    # OTP CORRECT
    # =========================================================

    db_user = Admin.query.filter_by(
        username=username
    ).first()


    if db_user is None:

        session.clear()

        return make_response(
            "Admin account not found!",
            404
        )


    # Login successful
    login_user(
        db_user,
        remember=True
    )


    # Clear OTP session
    session.pop(
        'admin_login_otp_hash',
        None
    )

    session.pop(
        'admin_login_user',
        None
    )

    session.pop(
        'admin_login_otp_expires',
        None
    )


    Log(
        "admin",
        "Admin logged in using password + email verification"
    )


    return redirect("/admin")

@login_required
@app.route("/admin/home")
def AdminHome():
	if not isAdmin(current_user):
		return abort(404)

	adminActivity = AdminActivity.query.order_by(AdminActivity.time).all()
	#convert time
	for log in adminActivity:
		log.time = datetime.fromtimestamp(log.time, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S GMT')

	#reverse list
	adminActivity.reverse()

	return render_template('admin_home.html', Activity=adminActivity)

@login_required
@app.route("/admin/versions" , methods=['GET', 'POST'])
def AdminVersions():
	if not isAdmin(current_user):
		return abort(404)

	if request.method == 'GET':

		return render_template('admin_versions.html' , pc_version=open("data/persist/version.txt", "r").read(), android_version=open("data/persist/android_version.txt", "r").read())
	elif request.method == 'POST':
		form = request.form
		form = {k: v[0] if len(v) == 1 else v for k, v in form.items()}

		if "pc_version" not in form or form["pc_version"] == "":
			return make_response("Invalid PC version!", 400)
		if "android_version" not in form or form["android_version"] == "":
			return make_response("Invalid Android version!", 400)

		#update version.txt
		with open("data/persist/version.txt", "w") as f:
			f.write(form["pc_version"])
		with open("data/persist/android_version.txt", "w") as f:
			f.write(form["android_version"])

		return redirect("/admin/versions")

@login_required
@app.route("/admin/server")
def AdminServer():
	if not isAdmin(current_user):
		return abort(404)

	#create backup folder if it doesn't exist
	if not os.path.exists("backup"):
		os.makedirs("backup")

	#get last backup in folder
	last_backup_time = 0
	last_backup_file = ""
	for file in os.listdir("backup"):
		if file.endswith(".zip"):
			file_time = int(file.replace(".zip", "").replace("-", "").replace("_", ""))
			if file_time > last_backup_time:
				last_backup_time = file_time
				last_backup_file = file.replace(".zip", "")

	if last_backup_file == "":
		last_backup = "Never"
	else:
		last_backup = time_ago_string(datetime.strptime(last_backup_file, "%Y-%m-%d_%H-%M-%S"))

	return render_template('admin_server.html', last_backup=last_backup)

# DATABASE MANAGER (Primary / LocalInstance / Archive)
# The manager inspects non-primary databases without changing
# the existing SQLAlchemy session or game persistence routing.
# ============================================================
DATABASE_MANAGER_ROOT = os.path.join(app.root_path, "localinstance")
DATABASE_MANIFEST_PATH = os.path.join(DATABASE_MANAGER_ROOT, "manifest.json")
PRIMARY_DATABASE_PATH = os.path.join(app.instance_path, "cardwarskingdom.db")

def _ensure_database_manifest():
    os.makedirs(DATABASE_MANAGER_ROOT, exist_ok=True)
    if not os.path.isfile(DATABASE_MANIFEST_PATH):
        initial = {
            "version": 1,
            "databases": [
                {"id": "local", "name": "LocalInstance", "type": "local",
                 "file": "cardwarskingdom_local.db", "enabled": True},
                {"id": "archive", "name": "Archive", "type": "archive",
                 "directory": "archive", "enabled": True}
            ]
        }
        with open(DATABASE_MANIFEST_PATH, "w", encoding="utf-8") as stream:
            json.dump(initial, stream, indent=2)

def _read_database_manifest():
    _ensure_database_manifest()
    with open(DATABASE_MANIFEST_PATH, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or not isinstance(data.get("databases"), list):
        raise ValueError("localinstance/manifest.json must contain a databases array")
    return data

def _database_manager_entries():
    manifest = _read_database_manifest()
    entries = [{
        "id": "primary", "name": "Primary", "type": "primary",
        "path": PRIMARY_DATABASE_PATH, "writable": True
    }]
    for item in manifest["databases"]:
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        db_id = str(item.get("id", ""))
        if db_id == "local":
            filename = str(item.get("file", "cardwarskingdom_local.db"))
            if os.path.basename(filename) != filename or not filename.endswith(".db"):
                continue
            path = os.path.join(DATABASE_MANAGER_ROOT, filename)
            entries.append({"id": "local", "name": str(item.get("name", "LocalInstance")),
                            "type": "local", "path": path, "writable": True})
        elif db_id == "archive":
            archive_dir = os.path.join(DATABASE_MANAGER_ROOT, "archive")
            os.makedirs(archive_dir, exist_ok=True)
            for filename in sorted(os.listdir(archive_dir), reverse=True):
                if filename.endswith(".db") and os.path.isfile(os.path.join(archive_dir, filename)):
                    entries.append({"id": "archive:" + filename,
                                    "name": str(item.get("name", "Archive")) + " — " + filename,
                                    "type": "archive", "path": os.path.join(archive_dir, filename),
                                    "writable": False})
    return entries

def _database_summary(path):
    summary = {"exists": os.path.isfile(path), "size": 0, "tables": [], "player_count": None}
    if not summary["exists"]:
        return summary
    summary["size"] = os.path.getsize(path)
    try:
        connection = sqlite3.connect("file:" + os.path.abspath(path) + "?mode=ro", uri=True, timeout=3)
        try:
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            summary["tables"] = [row[0] for row in rows]
            if "player" in summary["tables"]:
                summary["player_count"] = connection.execute('SELECT COUNT(*) FROM "player"').fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
    return summary

@app.route("/admin/databases", methods=["GET", "POST"])
@login_required
def AdminDatabases():
    if not isAdmin(current_user):
        return abort(404)
    message = None
    error = None
    if request.method == "POST":
        action = request.form.get("action", "")
        source_id = request.form.get("database_id", "primary")
        entries = _database_manager_entries()
        source = next((item for item in entries if item["id"] == source_id), None)
        if action != "create_archive":
            error = "Unknown database action."
        elif source is None or not os.path.isfile(source["path"]):
            error = "The selected database file does not exist."
        elif source["type"] == "archive":
            error = "An archive cannot be archived again from this screen."
        else:
            try:
                archive_dir = os.path.join(DATABASE_MANAGER_ROOT, "archive")
                os.makedirs(archive_dir, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                archive_name = source["id"] + "_" + stamp + ".db"
                destination = os.path.join(archive_dir, archive_name)
                # SQLite backup API ensures a consistent snapshot, even with WAL enabled.
                src_conn = sqlite3.connect(source["path"], timeout=10)
                dst_conn = sqlite3.connect(destination, timeout=10)
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
                    src_conn.close()
                Log("admin", current_user.username + " created database archive: " + archive_name)
                message = "Archive created: " + archive_name
            except Exception as exc:
                app.logger.exception("Database archive failed")
                error = "Archive failed: " + str(exc)
    entries = _database_manager_entries()
    databases = []
    selected_id = request.args.get("database", "primary")
    for entry in entries:
        row = dict(entry)
        row.update(_database_summary(entry["path"]))
        databases.append(row)
    selected = next((item for item in databases if item["id"] == selected_id), databases[0])
    players = []
    query = request.args.get("q", "").strip()
    if selected["exists"] and "player" in selected["tables"]:
        try:
            connection = sqlite3.connect("file:" + os.path.abspath(selected["path"]) + "?mode=ro", uri=True, timeout=3)
            connection.row_factory = sqlite3.Row
            try:
                columns = {row[1] for row in connection.execute('PRAGMA table_info("player")')}
                wanted = [name for name in ("username", "multiplayer_name", "leader_level", "level", "last_online") if name in columns]
                if "username" in columns and wanted:
                    sql = 'SELECT ' + ", ".join('"' + name + '"' for name in wanted) + ' FROM "player"'
                    params = []
                    if query:
                        sql += ' WHERE CAST("username" AS TEXT) LIKE ?'
                        params.append("%" + query[:100] + "%")
                    sql += ' ORDER BY "username" LIMIT 100'
                    players = [dict(row) for row in connection.execute(sql, params).fetchall()]
            finally:
                connection.close()
        except sqlite3.Error as exc:
            error = error or ("Could not read player list: " + str(exc))
    return render_template("admin_databases.html", databases=databases, selected=selected,
                           players=players, query=query, message=message, error=error)



def time_ago_string(date_time):
    now = datetime.now()
    time_difference = now - date_time

    # Extracting hours and minutes
    hours = time_difference.seconds // 3600
    minutes = (time_difference.seconds // 60) % 60

    if time_difference.days > 0:
        return f"{time_difference.days} days ago"
    elif hours > 0:
        return f"{hours} {'hour' if hours == 1 else 'hours'} ago"
    elif minutes > 0:
        return f"{minutes} {'minute' if minutes == 1 else 'minutes'} ago"
    else:
        return f"{time_difference.seconds} seconds ago"

@login_required
@app.route("/admin/server/backup")
def AdminBackup():
	if not isAdmin(current_user):
		return abort(404)

	backup = Backup()
	if not backup:
		return make_response("Failed to backup", 400)
	return redirect("/admin/server")

@login_required
@app.route("/admin/server/pull")
def AdminGitPull():
	if not isAdmin(current_user):
		return abort(404)

	Log("admin", current_user.username + " pulled from git.")

	#Git pull and return response
	output = subprocess.check_output(["git", "pull"])

	Log("admin", "Pulled from git. Output: " + output.decode("utf-8"))

	#TODO: restart server

	return make_response(output.decode("utf-8"), 200)

@login_required
@app.route("/admin/createadmin", methods=['GET', 'POST'])
def AdminCreateAdmin():
	if not isAdmin(current_user):
		return abort(404)

	if request.method == 'GET':
		return render_template('admin_createadmin.html')

	if request.method == 'POST':
		username = request.form['username']
		rank = request.form['rank']

		#create random password
		password = secrets.token_urlsafe(24)

		new_admin = Admin(
			username=username,
			password=bcrypt.generate_password_hash(password).decode('utf-8'),
			rank=int(rank),

			# New security fields
			email=None,
			email_verified=False,
			passkey_hash=None,
			verification_code_hash=None,
			verification_code_expires=None
		)

		db.session.add(new_admin)
		db.session.commit()

		Log(
			"admin",
			current_user.username +
			" created admin: " +
			username +
			" with rank: " +
			rank
		)

		return "Admin created! Username: " + username + " Password: " + password

@app.route("/admin/secretprofile", methods=['GET', 'POST'])
@login_required
def AdminSecretProfile():

    # Make sure only admin users can access this page
    if not isAdmin(current_user):
        return abort(404)

    # Get the current admin account
    admin = Admin.query.filter_by(
        username=current_user.username
    ).first()

    if admin is None:
        return abort(404)

    # GET
    if request.method == 'GET':
        return render_template(
            'admin_secretprofile.html',
            admin=admin
        )

    action = request.form.get(
        'action',
        ''
    )

    # =========================================================
    # GENERATE PASS-KEY
    # =========================================================

    if action == 'generate_passkey':

        if admin.passkey_hash:

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error="Pass-Key has already been generated and cannot be changed."
            )

        part1 = ''.join(
            random.choices(
                string.ascii_uppercase + string.digits,
                k=6
            )
        )

        part2 = ''.join(
            random.choices(
                string.ascii_uppercase + string.digits,
                k=6
            )
        )

        part3 = ''.join(
            random.choices(
                string.ascii_uppercase + string.digits,
                k=6
            )
        )

        passkey = (
            f"{part1}-"
            f"{part2}-"
            f"{part3}"
        )

        admin.passkey_hash = (
            bcrypt.generate_password_hash(
                passkey
            ).decode('utf-8')
        )

        db.session.commit()

        Log(
            "admin",
            "Admin Pass-Key generated"
        )

        return render_template(
            'admin_secretprofile.html',
            admin=admin,
            generated_passkey=passkey
        )


    # =========================================================
    # CHANGE PASSWORD
    # =========================================================

    elif action == 'change_password':

        current_password = request.form.get(
            'current_password',
            ''
        )

        new_password = request.form.get(
            'new_password',
            ''
        )

        confirm_password = request.form.get(
            'confirm_password',
            ''
        )

        if not bcrypt.check_password_hash(
            admin.password,
            current_password
        ):

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error="Current password is incorrect."
            )

        if new_password != confirm_password:

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error="New passwords do not match."
            )

        if len(new_password) < 12:

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error="Password must be at least 12 characters long."
            )

        admin.password = (
            bcrypt.generate_password_hash(
                new_password
            ).decode('utf-8')
        )

        db.session.commit()

        Log(
            "admin",
            "Admin password changed"
        )

        return render_template(
            'admin_secretprofile.html',
            admin=admin,
            success="Password changed successfully."
        )


    # =========================================================
    # REQUEST EMAIL CHANGE OTP
    # =========================================================

    elif action == 'request_email_change':

        new_email = request.form.get(
            'new_email',
            ''
        ).strip().lower()

        if not new_email or '@' not in new_email:

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error="Please enter a valid email address."
            )

        if admin.email and new_email == admin.email.lower():

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error="This is already your current email address."
            )

        # Make sure SMTP configuration exists
        if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD:

            Log(
                "admin",
                "Email change OTP failed: SMTP configuration is missing."
            )

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error=(
                    "Email verification is currently unavailable. "
                    "The server email service is not configured."
                )
            )

        otp = f"{secrets.randbelow(1000000):06d}"

        otp_hash = hashlib.sha256(
            otp.encode('utf-8')
        ).hexdigest()

        session['email_change_otp_hash'] = otp_hash
        session['email_change_new_email'] = new_email
        session['email_change_otp_expires'] = (
            datetime.utcnow() + timedelta(minutes=5)
        ).timestamp()

        try:

            send_email_change_otp(
                new_email,
                otp
            )

        except smtplib.SMTPAuthenticationError as e:

            # SMTP authentication failed.
            # Usually caused by incorrect SMTP credentials
            # or missing/invalid Gmail App Password.

            session.pop(
                'email_change_otp_hash',
                None
            )

            session.pop(
                'email_change_new_email',
                None
            )

            session.pop(
                'email_change_otp_expires',
                None
            )

            print(
                "[EMAIL AUTH ERROR]",
                repr(e)
            )

            Log(
                "admin",
                "Email change OTP failed: SMTP authentication failed. "
                "Check SMTP username/password or App Password configuration. "
                + repr(e)
            )

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error=(
                    "Email verification is currently unavailable. "
                    "The server email authentication is not configured correctly."
                )
            )

        except smtplib.SMTPException as e:

            session.pop(
                'email_change_otp_hash',
                None
            )

            session.pop(
                'email_change_new_email',
                None
            )

            session.pop(
                'email_change_otp_expires',
                None
            )

            print(
                "[SMTP ERROR]",
                repr(e)
            )

            Log(
                "admin",
                "Email change OTP failed: SMTP error: "
                + repr(e)
            )

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error=(
                    "Failed to send verification email "
                    "due to a server email error."
                )
            )

        except Exception as e:

            session.pop(
                'email_change_otp_hash',
                None
            )

            session.pop(
                'email_change_new_email',
                None
            )

            session.pop(
                'email_change_otp_expires',
                None
            )

            print(
                "[EMAIL ERROR]",
                repr(e)
            )

            Log(
                "admin",
                "Failed to send email change OTP: "
                + repr(e)
            )

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error="Failed to send verification email."
            )

        Log(
            "admin",
            "Email change OTP sent successfully to: "
            + new_email
        )

        return render_template(
            'admin_secretprofile.html',
            admin=admin,
            email_change_otp_sent=True,
            email_change_email=new_email,
            success=(
                "A 6-digit verification code has been sent "
                "to your new email address."
            )
        )


    # =========================================================
    # VERIFY EMAIL CHANGE OTP
    # =========================================================

    elif action == 'verify_email_change':

        otp = request.form.get(
            'otp',
            ''
        ).strip()

        stored_hash = session.get(
            'email_change_otp_hash'
        )

        new_email = session.get(
            'email_change_new_email'
        )

        expires_at = session.get(
            'email_change_otp_expires'
        )

        if not stored_hash or not new_email or not expires_at:

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error="No active email verification request found."
            )

        if datetime.utcnow().timestamp() > float(expires_at):

            session.pop(
                'email_change_otp_hash',
                None
            )

            session.pop(
                'email_change_new_email',
                None
            )

            session.pop(
                'email_change_otp_expires',
                None
            )

            Log(
                "admin",
                "Email change OTP expired."
            )

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                error=(
                    "The verification code has expired. "
                    "Please request a new code."
                )
            )

        if len(otp) != 6 or not otp.isdigit():

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                email_change_otp_sent=True,
                email_change_email=new_email,
                error="Verification code must be 6 digits."
            )

        entered_hash = hashlib.sha256(
            otp.encode('utf-8')
        ).hexdigest()

        if not secrets.compare_digest(
            entered_hash,
            stored_hash
        ):

            Log(
                "admin",
                "Invalid email change OTP entered."
            )

            return render_template(
                'admin_secretprofile.html',
                admin=admin,
                email_change_otp_sent=True,
                email_change_email=new_email,
                error="Invalid verification code."
            )

        # =====================================================
        # OTP VALID
        # =====================================================

        # Update email
        admin.email = new_email

        # Email is verified because the user
        # successfully received and entered the OTP.
        admin.email_verified = True

        db.session.commit()

        # Clear OTP session
        session.pop(
            'email_change_otp_hash',
            None
        )

        session.pop(
            'email_change_new_email',
            None
        )

        session.pop(
            'email_change_otp_expires',
            None
        )

        Log(
            "admin",
            "Admin email changed and verified successfully."
        )

        return render_template(
            'admin_secretprofile.html',
            admin=admin,
            success="Email address changed successfully."
        )


    # =========================================================
    # INVALID ACTION
    # =========================================================

    return render_template(
        'admin_secretprofile.html',
        admin=admin,
        error="Invalid action."
    )

@app.route("/admin/players")
@login_required
def AdminPlayers():
	if not isAdmin(current_user):
		return abort(404)

	# Load Primary through the normal ORM, then explicitly read LocalInstance.
	# PlayerDatabaseRoutingSession intentionally routes only when a request names
	# a particular player; an unqualified Player.query.all() only sees Primary.
	players_by_username = {}
	for player_obj in Player.query.all():
		player_data = player_obj.as_dict()
		player_data["source_database"] = "Primary"
		players_by_username[str(player_data.get("username", ""))] = player_data

	local_path = _local_instance_db_path()
	if local_path and os.path.isfile(local_path):
		try:
			with sqlite3.connect(local_path, timeout=5) as connection:
				connection.row_factory = sqlite3.Row
				tables = {
					row[0] for row in connection.execute(
						"SELECT name FROM sqlite_master WHERE type='table'"
					)
				}
				if "player" in tables:
					columns = {
						row[1] for row in connection.execute('PRAGMA table_info("player")')
					}
					if "username" in columns:
						for row in connection.execute('SELECT * FROM "player"'):
							player_data = dict(row)
							username = str(player_data.get("username") or "").strip()
							if not username:
								continue
							# Supply optional fields absent from older LocalInstance schemas.
							for column in Player.__table__.columns:
								player_data.setdefault(column.name, None)
							player_data["source_database"] = "LocalInstance"
							# LocalInstance wins if the same ID exists in both databases.
							players_by_username[username] = player_data
					else:
						app.logger.warning(
							"[AdminPlayers] LocalInstance has no username column: %s",
							local_path
						)
				else:
					app.logger.warning(
							"[AdminPlayers] LocalInstance has no player table: %s",
							local_path
						)
		except (OSError, sqlite3.Error):
			app.logger.exception("[AdminPlayers] Could not read LocalInstance: %s", local_path)

	players = list(players_by_username.values())

	# Keep the existing list rules: only saved, levelled, non-banned players.
	players = [
		player for player in players
		if player.get("game") is not None and player.get("leader_level") is not None
	]
	players = [
		player for player in players
		if player.get("username") and not IsUserBanned(player["username"])
	]

	for player in players:
		last_online = player.get("last_online")
		try:
			player["last_online"] = datetime.fromtimestamp(
				int(last_online or 0), tz=timezone.utc
				).strftime('%Y-%m-%d %H:%M:%S UTC')
		except (TypeError, ValueError, OSError, OverflowError):
			player["last_online"] = "Unknown"

		if not player.get("multiplayer_name"):
			player["multiplayer_name"] = GetNameFromSave(player.get("game"))

	sortQuery = request.args.get('sort')
	if sortQuery:
		players = sorted(
			players,
			key=lambda player: (
				player.get(sortQuery) is not None,
				str(player.get(sortQuery) or "").casefold()
			),
			reverse=True
		)
	else:
		players = players[::-1]

	return render_template(
		'admin_players.html',
		players=players,
		player_count=len(players)
	)

def GetNameFromSave(save):

	try:
		game = DecryptGameData(save)
	except Exception:
		return None
	if game is None:
		return None
	return game.get("MultiplayerPlayerName")

@app.route("/online_players")
def AdminOnlinePlayers():


    players = Player.query.all()
    players = [player.as_dict() for player in players]

    # Get the current time minus five minutes (in UTC)
    current_time = datetime.now(timezone.utc)
    five_minutes_ago = current_time - timedelta(minutes=5)

    online_players = []

    # Log and check player last_online times
    for player in players:
        # Convert any bytes fields to string (e.g., multiplayer_name, game, etc.)
        for key, value in player.items():
            if isinstance(value, bytes):
                player[key] = value.decode('utf-8')  # Decode bytes to string

        if player["last_online"]:
            last_online = datetime.fromtimestamp(player["last_online"], tz=timezone.utc)
            if last_online >= five_minutes_ago:
                online_players.append(player)

    return {
        "online_player_count": len(online_players)
    }


@app.route("/admin/players/<player>")
@login_required
def AdminPlayer(player):
	if not isAdmin(current_user):
		return abort(404)

	source_database = "LocalInstance" if _local_instance_has_player(player) else "Primary"
	player = Player.query.filter_by(username=player).first()

	if player is None:
		return make_response("No player found!", 404)

	player = player.as_dict()

	player["last_online"] = datetime.fromtimestamp(player["last_online"], tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S GMT')

	game = None
	try:
		game = DecryptGameData(player["game"])
		if player["multiplayer_name"] == None:
			player["multiplayer_name"] = game["MultiplayerPlayerName"]
	except Exception:
		Log("admin", "Failed to decrypt player game data for player: " + player["username"])
		game = None

	if game is None:
		return render_template('admin_player.html', player=player, source_database=source_database)

	battle_history = game["BattleHistory"]
	battle_history.sort(key=lambda x: x["recordTime"])

	for battle in battle_history:
		battle["recordTime"] = datetime.fromtimestamp(battle["recordTime"], tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S GMT')

	#fix device name
	if player["devicename"] is not None:
		player["devicename"] = re.sub(r'%[0-9A-Fa-f]{2}', lambda m: chr(int(m.group(0)[1:], 16)), player["devicename"])

	Inventory = game["Inventory"]

	#remove all items that are not creatures
	if Inventory is not None:
		Inventory = [item for item in Inventory if item["_T"] == "CR"]

	return render_template('admin_player.html', player=player, source_database=source_database, is_banned=IsUserBanned(player["username"]), SoftCurrency=game.get("SoftCurrency"), HardCurrency=int(game.get("PaidHardCurrency", 0) or 0) + int(game.get("FreeHardCurrency", 0) or 0), PvpCurrency=game.get("PvpCurrency"), InstalledDate=datetime.fromtimestamp(game["InstalledDate"], tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S GMT') if game.get("InstalledDate") else None, PVPBanned=bool(game.get("Zxcvbnm", False)), MultiplayerLevel=game.get("MultiplayerLevel"), InventorySpace=game.get("InventorySpace"), BattleHistory=battle_history, DeviceName=player.get("devicename"), Inventory=Inventory)

@app.route("/admin/players/<player>/game")
@login_required
def AdminPlayerGame(player):

    source_database = "LocalInstance" if _local_instance_has_player(player) else "Primary"

    print("========================================")
    print("[AdminPlayerGame] ROUTE HIT")
    print("[AdminPlayerGame] player:", repr(player))
    print("[AdminPlayerGame] current_user:", current_user.username)
    print("========================================")

    if not isAdmin(current_user):
        print("[AdminPlayerGame] NOT ADMIN")
        return abort(404)

    player_obj = Player.query.filter_by(username=player).first()

    print(
        "[AdminPlayerGame] player found:",
        player_obj is not None
    )

    if player_obj is None:
        print(
            "[AdminPlayerGame] PLAYER NOT FOUND:",
            repr(player)
        )
        return make_response("No player found!", 404)

    player = player_obj.as_dict()

    try:
        # Supports both raw JSON saves and the legacy data=Base64(GZIP(JSON)) format.
        game, _save_format = _decode_game_save_for_name_change(player_obj.game)
    except Exception as decode_error:
        Log("admin", "Failed to decode game data for player " + player_obj.username + ": " + str(decode_error))
        return make_response("Player game data is invalid or unsupported. Original data was not changed.", 422)

    # Render a real JSON object in the editor; do not stringify the save twice.
    game_json = json.dumps(game, ensure_ascii=False, indent=2, default=lambda x:
        x.decode("utf-8", errors="ignore") if isinstance(x, bytes) else str(x)
    )

    return render_template(
        'admin_player_game.html',
        game=game_json,
        player_id=player["username"],
        source_database=source_database
    )

@app.route("/admin/players/<player>/game/edit", methods=['POST'])
@login_required
def AdminPlayerGameEdit(player):
    if not isAdmin(current_user):
        return abort(404)

    player = Player.query.filter_by(username=player).first()

    if player is None:
        return make_response("No player found!", 404)

    # Accept only a JSON object and preserve the player's existing storage format.
    submitted_game = request.form.get("player_game")
    if submitted_game is None or not submitted_game.strip():
        return make_response("No game data provided!", 400)

    try:
        save_data = _loads_legacy_player_json(submitted_game)
        if not isinstance(save_data, dict):
            return make_response("Game data must be a JSON object.", 400)

        # Keep raw JSON saves as JSON and encoded persistence saves encoded.
        try:
            _existing_data, save_format = _decode_game_save_for_name_change(player.game)
        except Exception as decode_error:
            # Do not overwrite an unreadable save unless its storage format is clear.
            existing_text = player.game.decode("utf-8", errors="strict") if isinstance(player.game, (bytes, bytearray)) else str(player.game or "")
            if existing_text.lstrip().startswith("{"):
                save_format = "json"
            elif "data=" in existing_text:
                save_format = ("encoded", existing_text.find("data="), len("data="))
            else:
                return make_response("Existing game data format is invalid; original data was not changed.", 422)

        updated_game = _encode_game_save_with_name(player.game, save_data, save_format)
        # Verify the generated save can be decoded before committing it.
        verified_data, _verified_format = _decode_game_save_for_name_change(updated_game)
        if not isinstance(verified_data, dict):
            return make_response("Game data validation failed; original data was not changed.", 400)

        player.game = updated_game
        db.session.commit()
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as e:
        db.session.rollback()
        return make_response("Invalid game JSON; original data was not changed. " + str(e), 400)
    except Exception as e:
        db.session.rollback()
        Log("admin", "Failed to update game data for player " + player.username + ": " + str(e))
        return make_response("Failed to update game data; original data was not changed.", 500)

    Log("admin", current_user.username + " edited game data for player: " + player.username)
    return redirect("/admin/players/" + player.username)


def DecryptGameData(game: str):
    """Decode either a raw JSON save or the legacy data=Base64(GZIP(JSON)) payload."""
    if game is None or game == b"" or game == b" ":
        return None

    try:
        input_str = game.decode("utf-8") if isinstance(game, (bytes, bytearray)) else str(game)
        stripped = input_str.strip()

        # Admin/API writes may store the save as plain JSON.
        if stripped.startswith("{"):
            data = _loads_legacy_player_json(stripped)
            return data if isinstance(data, dict) else None

        # Reuse the persistence decoder for both legacy prefixes and URL-safe Base64.
        data, _save_format = _decode_game_save_for_name_change(input_str)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        Log("admin", "Failed to decode game data: " + str(exc))
        return None

@app.route("/admin/pvp_leaderboard")
@login_required
def AdminPvpLeaderboard():
    if not isAdmin(current_user):
        return abort(404)

    leaderboard = []

    for player in Player.query.all():
        try:
            game = DecryptGameData(player.game)

            if game is None:
                continue

            leaderboard.append({
                "username": player.username,
                "player_name": game.get("MultiplayerPlayerName", "Unknown"),
                "multiplayer_level": int(game.get("MultiplayerLevel", 999)),
                "points": int(game.get("PointsInMultiplayerLevel", 0)),
            })

        except Exception:
            continue

    # Önce MultiplayerLevel, sonra aynı level içinde puan
    leaderboard.sort(
        key=lambda x: (
            x["multiplayer_level"] == -1,  # False önce, True sonra
            x["multiplayer_level"],        # 1,2,3,...,26
            -x["points"]                   # Aynı level içinde yüksek puan önce
        )
    )

    return render_template(
        "admin_pvp_leaderboard.html",
        leaderboard=leaderboard
    )

# -----------------------------
# API endpoint for granting rewards (used by Discord bot)
# -----------------------------
# POST /api/grant_reward
# Body (JSON):
#   - cardwars_id: string (required)
#   - reward_type: string (required, currently only "Gems" supported)
#   - amount: integer (required)
#   - admin_user: string (optional if using API key)
#   - admin_pass: string (optional if using API key)
#
# Or send header: X-API-KEY: <PYAN_API_KEY>
#
# IMPORTANT: This file does not contain any hardcoded credentials. Set the following
# environment variables on PythonAnywhere (recommended):
#   - PYAN_API_KEY          (preferred)  -> bot should send this in X-API-KEY header
#   - PYAN_ADMIN_USER       (optional)  -> fallback to admin_user/admin_pass in JSON
#   - PYAN_ADMIN_PASS       (optional)
#
# On the bot (Railway) put bot credentials there (DB_LOGIN, DB_PASSWORD) and the bot
# will send them in the request body (or use an API key approach).
@app.route("/api/grant_reward", methods=['POST'])
def ApiGrantReward():
    try:
        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({'success': False, 'error': 'JSON body required'}), 400

        # AUTH: prefer API key header, fallback to admin_user/admin_pass
        api_key_header = request.headers.get("X-API-KEY")
        configured_api_key = os.environ.get("PYAN_API_KEY")
        authorized = False

        if configured_api_key and api_key_header and api_key_header == configured_api_key:
            authorized = True
        else:
            # fallback to user/pass check
            admin_user = payload.get("admin_user")
            admin_pass = payload.get("admin_pass")
            expected_user = os.environ.get("PYAN_ADMIN_USER")
            expected_pass = os.environ.get("PYAN_ADMIN_PASS")
            if expected_user and expected_pass:
                if admin_user == expected_user and admin_pass == expected_pass:
                    authorized = True
                else:
                    authorized = False
            else:
                # If no expected creds are configured on server AND no API key,
                # deny to avoid accidental open endpoint.
                return jsonify({'success': False, 'error': 'Server auth not configured (set PYAN_API_KEY or PYAN_ADMIN_USER/PYAN_ADMIN_PASS)'}), 500

        if not authorized:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401

        # validate payload
        cardwars_id = payload.get("cardwars_id")
        reward_type = payload.get("reward_type")
        amount = payload.get("amount")

        if not cardwars_id or not reward_type or amount is None:
            return jsonify({'success': False, 'error': 'cardwars_id, reward_type and amount are required'}), 400
        try:
            amount = int(amount)
        except Exception:
            return jsonify({'success': False, 'error': 'amount must be an integer'}), 400
        if amount <= 0:
            return jsonify({'success': False, 'error': 'amount must be > 0'}), 400

        # currently only Gems supported (extend as needed)
        if reward_type != "Gems":
            return jsonify({'success': False, 'error': f'Unsupported reward_type: {reward_type}'}), 400

        # find player by multiplayer_name OR username, try case-insensitive match
        player = Player.query.filter_by(multiplayer_name=cardwars_id).first()
        if player is None:
            player = Player.query.filter_by(username=cardwars_id).first()
        if player is None:
            # try lower-case match on multiplayer_name
            player = Player.query.filter(func.lower(Player.multiplayer_name) == cardwars_id.lower()).first()

        if player is None:
            return jsonify({'success': False, 'error': 'Player not found'}), 404

        # get current save (player.game)
        game_field = player.game
        if game_field is None:
            save_json = {}
        else:
            # decode bytes if needed
            game_text = None
            if isinstance(game_field, (bytes, bytearray)):
                try:
                    game_text = game_field.decode('utf-8')
                except Exception:
                    game_text = None
            else:
                game_text = game_field

            save_json = None
            # try parse as JSON string
            if isinstance(game_text, str) and game_text.strip().startswith('{'):
                try:
                    save_json = json.loads(game_text)
                except Exception:
                    save_json = None

            if save_json is None:
                # try decrypting using existing DecryptGameData
                try:
                    decrypted = DecryptGameData(game_field)
                    if isinstance(decrypted, dict):
                        save_json = decrypted
                    else:
                        if isinstance(decrypted, str) and decrypted.strip().startswith('{'):
                            save_json = json.loads(decrypted)
                except Exception:
                    save_json = None

            if save_json is None:
                # as last resort, initialize empty save
                save_json = {}

        # ensure FreeHardCurrency exists and is integer
        try:
            prev_amount = int(save_json.get("FreeHardCurrency", 0) or 0)
        except Exception:
            prev_amount = 0
        new_amount = prev_amount + amount
        save_json["FreeHardCurrency"] = new_amount

        # write back save as JSON string (AdminPlayerGameEdit-like behavior)
        try:
            player.game = json.dumps(save_json, ensure_ascii=False)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': f'Failed to write save: {str(e)}'}), 500

        # Optional: log admin activity
        Log("admin", f"API grant: +{amount} FreeHardCurrency to {player.username} (by API)")

        return jsonify({
            'success': True,
            'player': player.username,
            'previous': prev_amount,
            'new': new_amount
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': f'Internal error: {str(e)}'}), 500

        # -----------------------------
# API endpoint for adding a creature (used by Discord bot)
# -----------------------------
# POST /api/add_creature
# Body (JSON):
#   - cardwars_id: string (required)
#   - creature_id: string (required)
#   - star_rating: integer (optional, 1..5)
#   - admin_user: string (optional if using API key)
#   - admin_pass: string (optional if using API key)
#
# Or header: X-API-KEY: <PYAN_API_KEY>
@app.route("/api/add_creature", methods=['POST'])
def ApiAddCreature():
    try:
        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({'success': False, 'error': 'JSON body required'}), 400

        # AUTH: prefer API key header, fallback to admin_user/admin_pass
        api_key_header = request.headers.get("X-API-KEY")
        configured_api_key = os.environ.get("PYAN_API_KEY")
        authorized = False

        if configured_api_key and api_key_header and api_key_header == configured_api_key:
            authorized = True
        else:
            admin_user = payload.get("admin_user")
            admin_pass = payload.get("admin_pass")
            expected_user = os.environ.get("PYAN_ADMIN_USER")
            expected_pass = os.environ.get("PYAN_ADMIN_PASS")
            if expected_user and expected_pass:
                if admin_user == expected_user and admin_pass == expected_pass:
                    authorized = True
                else:
                    authorized = False
            else:
                # If no expected creds are configured on server AND no API key,
                # deny to avoid accidental open endpoint.
                return jsonify({'success': False, 'error': 'Server auth not configured (set PYAN_API_KEY or PYAN_ADMIN_USER/PYAN_ADMIN_PASS)'}), 500

        if not authorized:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401

        cardwars_id = payload.get("cardwars_id")
        creature_id = payload.get("creature_id")
        star_rating = payload.get("star_rating", None)

        if not cardwars_id or not creature_id:
            return jsonify({'success': False, 'error': 'cardwars_id and creature_id are required'}), 400

        # validate star_rating if provided
        if star_rating is not None:
            try:
                star_rating = int(star_rating)
            except Exception:
                return jsonify({'success': False, 'error': 'star_rating must be an integer'}), 400
            if star_rating < 1 or star_rating > 5:
                return jsonify({'success': False, 'error': 'star_rating must be between 1 and 5'}), 400
        else:
            star_rating = 1

        # find player by multiplayer_name OR username (case-insensitive similar to grant)
        player = Player.query.filter_by(multiplayer_name=cardwars_id).first()
        if player is None:
            player = Player.query.filter_by(username=cardwars_id).first()
        if player is None:
            player = Player.query.filter(func.lower(Player.multiplayer_name) == cardwars_id.lower()).first()

        if player is None:
            return jsonify({'success': False, 'error': 'Player not found'}), 404

        # load player's save (player.game) with same method as grant endpoint
        game_field = player.game
        save_json = None

        if game_field is None:
            save_json = {}
        else:
            game_text = None
            if isinstance(game_field, (bytes, bytearray)):
                try:
                    game_text = game_field.decode('utf-8')
                except Exception:
                    game_text = None
            else:
                game_text = game_field

            # try parse JSON
            if isinstance(game_text, str) and game_text.strip().startswith('{'):
                try:
                    save_json = json.loads(game_text)
                except Exception:
                    save_json = None

            if save_json is None:
                try:
                    decrypted = DecryptGameData(game_field)
                    if isinstance(decrypted, dict):
                        save_json = decrypted
                    else:
                        if isinstance(decrypted, str) and decrypted.strip().startswith('{'):
                            save_json = json.loads(decrypted)
                except Exception:
                    save_json = None

        if save_json is None:
            save_json = {}

        # ensure Inventory array exists
        inventory = save_json.get("Inventory")
        if inventory is None or not isinstance(inventory, list):
            inventory = []
            save_json["Inventory"] = inventory

        # compute new UniqueID: max present UniqueID + 1 (or 1 if none)
        max_uid = 0
        for it in inventory:
            try:
                uid = int(it.get("UniqueID", 0))
                if uid > max_uid:
                    max_uid = uid
            except Exception:
                continue
        new_uid = max_uid + 1 if max_uid >= 0 else 1

        # build creature object
        creature_obj = {
            "_T": "CR",
            "ID": str(creature_id),
            "UniqueID": int(new_uid),
            "Xp": 0,
            "Favorite": 0,
            "Passive": 1,
            "PassiveFeeds": 0,
            "StarRating": int(star_rating)
        }

        # append and save
        inventory.append(creature_obj)

        # write back save as JSON string
        try:
            player.game = json.dumps(save_json, ensure_ascii=False)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': f'Failed to write save: {str(e)}'}), 500

        # log admin activity
        Log("admin", f"API add creature: {creature_id} (Star {star_rating}) UniqueID={new_uid} to {player.username}")

        return jsonify({
            'success': True,
            'player': player.username,
            'unique_id': new_uid,
            'starRating': star_rating,
            'previous_inventory_count': max_uid and (len(inventory)-1) or 0,
            'new_inventory_count': len(inventory)
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': f'Internal error: {str(e)}'}), 500


# Supported temporary ban durations in seconds.
BAN_DURATIONS = {
	"30m": 30 * 60,
	"1h": 1 * 60 * 60,
	"2h": 2 * 60 * 60,
	"4h": 4 * 60 * 60,
	"6h": 6 * 60 * 60,
	"8h": 8 * 60 * 60,
	"12h": 12 * 60 * 60,
	"16h": 16 * 60 * 60,
	"24h": 24 * 60 * 60,
	"2d": 2 * 24 * 60 * 60,
	"3d": 3 * 24 * 60 * 60,
	"4d": 4 * 24 * 60 * 60,
	"7d": 7 * 24 * 60 * 60,
	"2w": 14 * 24 * 60 * 60,
	"1mo": 30 * 24 * 60 * 60,
}


def CreateOrUpdatePlayerBan(player, duration_key=None):
	"""Create/update a player ban. None means permanent."""
	now = int(time.time())
	ban = Bans.query.filter_by(username=player).first()

	if ban is None:
		ban = Bans(
			username=player,
			bantype="userid",
			author=current_user.username,
			time=now
		)
		db.session.add(ban)

	ban.bantype = "userid"
	ban.author = current_user.username
	ban.time = now

	if duration_key is None or duration_key == "permanent":
		ban.expires_at = None
		return "permanent"

	if duration_key not in BAN_DURATIONS:
		raise ValueError("Invalid ban duration")

	ban.expires_at = now + BAN_DURATIONS[duration_key]
	return duration_key


@login_required
@app.route("/admin/players/<player>/<action>")
@app.route("/admin/players/<player>/<action>/<duration>")
def AdminPlayerAction(player, action, duration=None):
	if not isAdmin(current_user):
		return abort(404)

	if action == "ban":
		# Old /ban URL remains permanent for compatibility.
		try:
			ban_type = CreateOrUpdatePlayerBan(player, duration)
		except ValueError:
			return make_response("Invalid ban duration!", 400)

		db.session.commit()

		message = (
			current_user.username + " banned ID: " + player +
			(" permanently" if ban_type == "permanent" else " for " + ban_type)
		)

	elif action == "unban":
		player_check = Bans.query.filter_by(username=player).first()

		if player_check is not None:
			db.session.delete(player_check)
			db.session.commit()

		message = current_user.username + " unbanned ID: " + player

	else:
		return make_response("Invalid action!", 400)

	Log("admin", message)
	DiscordWebhookMessage(message)
	return redirect("/admin/players/" + player)

def SystemBan(username):
	Log("admin", "SYSTEM BANNED " + username)
	if not IsUserBanned(username):
		newban = Bans(username=username, bantype="userid", author="SYSTEM", time=int(time.time()))
		db.session.add(newban)
		db.session.commit()
		DiscordWebhookMessage("SYSTEM performed ban on ID: " + username)


@app.route("/admin/players/transfer", methods=['POST'])
def AdminPlayerTransfer():
    # Fetch API key from the request headers
    api_key = request.headers.get("API-Key")

    # Validate the API key
    if api_key != "9df81b2c-4f3a-41c7-8a3e-e28c0f6d9c49":
        return jsonify({"error": "Unauthorized access"}), 403

    # Get the player IDs and leader_level from the request
    data = request.json
    id1 = data.get("id1")
    id2 = data.get("id2")
    leader_level = data.get("leader_level")

    if not id1 or not id2 or not leader_level:
        return jsonify({"error": "Both player IDs and leader level are required"}), 400

    # Check if the IDs are the same
    if id1 == id2:
        return jsonify({"error": "Player IDs cannot be the same for both players"}), 400

    # Fetch players
    player1 = Player.query.filter_by(username=id1).first()
    player2 = Player.query.filter_by(username=id2).first()

    if not player1 or not player2:
        return jsonify({"error": "One or both players not found"}), 404

    try:
        # Convert the leader_level to an integer (if it isn't already)
        leader_level = int(leader_level.strip())  # Strip any extra spaces and convert to integer

        # Check if the leader level matches
        if player1.leader_level != leader_level:
            return jsonify({"error": f"Rank does not match for player {id1}. Make sure you enter the correct one."}), 400

        # Directly transfer the game data (without decryption)
        if player1.game:
            player2.game = player1.game  # Transfer the .bin file as is, no decryption needed
        else:
            logging.error(f"No game data found for {id1}")
            return jsonify({"error": "No game data to transfer from player 1"}), 400

        db.session.commit()

        logging.info(f"Transferred progress from {id1} to {id2}")
        return jsonify({"success": f"Progress transferred from {id1} to {id2}"}), 200

    except Exception as e:
        logging.error(f"Error during transfer: {str(e)}")
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

@login_required
@app.route("/admin/ipban/<ip>/unban")
def AdminIPBan(ip):
	if not isAdmin(current_user):
		return abort(404)

	Log("admin", current_user.username + " performed unban on " + ip)

	player_check = Bans.query.filter_by(username=ip).first()
	db.session.delete(player_check)
	db.session.commit()

	DiscordWebhookMessage(current_user.username + " performed unban on IP: " + ip)
	return redirect("/admin/bannedips")

@login_required
@app.route("/admin/ipban", methods=['POST'])
def AdminIPBanAction():
	if not isAdmin(current_user):
		return abort(404)

	Log("admin", current_user.username + " performed ban on " + request.form['ip'])

	newban = Bans(username=request.form['ip'], bantype="ip", author=current_user.username, time=int(time.time()))
	db.session.add(newban)
	db.session.commit()

	DiscordWebhookMessage(current_user.username + " performed ban on IP: " + request.form['ip'])
	return redirect("/admin/bannedips")


@login_required
@app.route("/admin/bannedplayers")
def AdminBannedPlayers():
	if not isAdmin(current_user):
		return abort(404)

	# Remove expired temporary bans before displaying the list.
	active_bans = []
	for ban in Bans.query.filter_by(bantype="userid").all():
		if IsBanActive(ban):
			active_bans.append(ban)
		else:
			db.session.delete(ban)

	db.session.commit()

	bans = [ban.as_dict() for ban in active_bans]

	#get player name
	for ban in bans:
		player_obj = Player.query.filter_by(username=ban["username"]).first()

		if player_obj is not None:
			ban["multiplayer_name"] = player_obj.multiplayer_name
			if ban["multiplayer_name"] is None:
				ban["multiplayer_name"] = GetNameFromSave(player_obj.game)
		else:
			ban["multiplayer_name"] = None

		ban["duration"] = "Permanent"

		if ban["expires_at"] is not None:
			ban["duration"] = datetime.fromtimestamp(
				ban["expires_at"],
				tz=timezone.utc
			).strftime('%Y-%m-%d %H:%M:%S GMT')

		if ban["time"] is not None:
			ban["time"] = datetime.fromtimestamp(
				ban["time"],
				tz=timezone.utc
			).strftime('%Y-%m-%d %H:%M:%S GMT')

	return render_template('admin_bannedplayers.html', bans=bans)

@login_required
@app.route("/admin/bannedips")
def AdminBannedIPs():
	if not isAdmin(current_user):
		return abort(404)

	bans = Bans.query.filter_by(bantype="ip").all()
	bans = [ban.as_dict() for ban in bans]

	for ban in bans:
		ban["time"] = datetime.fromtimestamp(ban["time"], tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S GMT')
	return render_template('admin_bannedips.html', bans=bans)

@login_required
@app.route("/admin/maintenance")
def AdminMaintenance():
	if not isAdmin(current_user):
		return abort(404)

	return render_template('admin_maintenance.html', maintenance=maintenance)

@login_required
@app.route("/admin/maintenance/<action>")
def AdminMaintenanceAction(action):
	if not isAdmin(current_user):
		return abort(404)

	global maintenance
	if action == "enable":
		maintenance = True
	elif action == "disable":
		maintenance = False
	Log("admin", current_user.username + " updated maintenance mode to " + ("on" if maintenance else "off"))
	return redirect("/admin/maintenance")

@app.route("/admin/logout")
def AdminLogout():
	logout_user()
	return redirect("/admin")

def Backup():
	#get date and time
	now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
	os.makedirs("backup/" + now, exist_ok=True)

	# copy database safely
	db_path = "instance/cardwarskingdom.db"
	if os.path.exists(db_path):
		shutil.copy(db_path, f"backup/{now}/cardwarskingdom.db")
	else:
		Log("admin", "Database file missing during backup!")
		return False

	#copy persist folder
	shutil.copytree("data/persist", f"backup/{now}/persist")

	#zip
	shutil.make_archive("backup/" + now, 'zip', "backup/" + now)

	#delete folder
	shutil.rmtree("backup/" + now)

	Log("admin", "Backed up")
	return True

@login_required
@app.route("/admin/misc")
def AdminMisc():
	if not isAdmin(current_user):
		return abort(404)

	return render_template('admin_misc.html')

@login_required
@app.route("/admin/logs/delete/olderthan/<days>")
def AdminLogsDeleteOlderThan(days):
	if not isAdmin(current_user):
		return abort(404)

	#convert days to seconds
	days = int(days)
	seconds = days * 86400

	#delete logs older than x days
	db.session.query(Logs).filter(Logs.time < int(time.time()) - seconds).delete()
	db.session.commit()

	Log("admin", current_user.username + " deleted logs older than " + str(days))

	return redirect("/admin/logs")

@login_required
@app.route("/admin/upsight/delete/olderthan/<days>")
def AdminUpsightDeleteOlderThan(days):
	if not isAdmin(current_user):
		return abort(404)

	#convert days to seconds
	days = int(days)
	seconds = days * 86400

	#delete logs older than x days
	db.session.query(UpsightLogs).filter(UpsightLogs.time < int(time.time()) - seconds).delete()
	db.session.commit()

	Log("admin", current_user.username + " deleted upsight logs older than " + str(days))

	return redirect("/admin/upsight")

@login_required
@app.route("/admin/logs", methods=['GET'])
def AdminLogs():
	if not isAdmin(current_user):
		return abort(404)

	perpage = 20
	pagerequest = request.args.get('page', 1, type=int)
	query = request.args.get('query', '', type=str)
	logs = db.paginate(db.select(Logs).order_by(Logs.id.desc()), page=pagerequest, per_page=perpage)

	if query != '':
		logs = db.paginate(db.select(Logs).filter(Logs.player == query).order_by(Logs.id.desc()), page=pagerequest, per_page=perpage)

	return render_template('admin_logs.html', logs=logs, query=query)

@login_required
@app.route("/admin/upsight", methods=['GET'])
def AdminUpsight():
	if not isAdmin(current_user):
		return abort(404)

	perpage = 20
	pagerequest = request.args.get('page', 1, type=int)
	query = request.args.get('query', '', type=str)
	logs = db.paginate(db.select(UpsightLogs).order_by(UpsightLogs.id.desc()), page=pagerequest, per_page=perpage)

	if query != '':
		logs = db.paginate(db.select(UpsightLogs).filter(UpsightLogs.player_id == query).order_by(UpsightLogs.id.desc()), page=pagerequest, per_page=perpage)

	#convert time
	for log in logs.items:
		log.time = datetime.fromtimestamp(log.time)

	return render_template('admin_upsight.html', logs=logs, query=query)

class Bans(db.Model):
	username = db.Column(db.String(80), primary_key=True)
	bantype = db.Column(db.String(80), nullable=False)
	author = db.Column(db.String(80), nullable=True)
	time = db.Column(db.Integer, nullable=True, default=int(time.time()))

	# None = permanent ban.
	# Unix timestamp = temporary ban expiration time.
	expires_at = db.Column(db.Integer, nullable=True, default=None)

	def as_dict(self):
		return {c.name: getattr(self, c.name) for c in self.__table__.columns}


def MigrateBanExpiration():
	"""Add expires_at to existing SQLite ban tables without deleting old bans."""
	from sqlalchemy import inspect, text

	inspector = inspect(db.engine)

	if "bans" not in inspector.get_table_names():
		return

	existing_columns = {
		column["name"]
		for column in inspector.get_columns("bans")
	}

	if "expires_at" not in existing_columns:
		with db.engine.begin() as connection:
			connection.execute(text(
				"ALTER TABLE bans ADD COLUMN expires_at INTEGER"
			))
		print("[Ban Migration] Added 'expires_at' column.")

class Logs(db.Model):
	id = db.Column(db.Integer, primary_key=True)
	date = db.Column(db.String(80), nullable=False)
	time = db.Column(db.String(80), nullable=False)
	player = db.Column(db.String(80), nullable=False)
	ip = db.Column(db.String(80), nullable=True)
	message = db.Column(db.String(8192), nullable=False)

class UpsightLogs(db.Model):
	id = db.Column(db.Integer, primary_key=True)
	player_id = db.Column(db.String(80), nullable=False)
	time = db.Column(db.Integer, nullable=False, default=int(time.time()))
	event = db.Column(db.String(80), nullable=False)
	action = db.Column(db.String(80), nullable=False)
	message = db.Column(db.String(1024), nullable=True)

def PlayerLog(ip:str,player:str, message:str):
	db_log = Logs(date=datetime.now().strftime("%Y-%m-%d"), time=datetime.now().strftime("%H:%M:%S"), player=player, ip=ip, message=message)
	db.session.add(db_log)
	db.session.commit()

def IPFromRequest(request:Request):
	ip = request.remote_addr
	if request.headers.getlist("X-Forwarded-For"):
		ip = request.headers.getlist("X-Forwarded-For")[0]
	return ip

class Player(db.Model):
	username = db.Column(db.String(80), primary_key=True, unique=True, nullable=False)
	game = db.Column(db.String(8192), nullable=True)
	multiplayer_name = db.Column(db.String(128), nullable=True)
	icon = db.Column(db.String(128), nullable=True)
	deck = db.Column(db.String(1024), nullable=True)
	deck_rank = db.Column(db.String(16), nullable=True)
	landscapes = db.Column(db.String(1024), nullable=True)
	helper_creature = db.Column(db.String(1024), nullable=True)
	leader = db.Column(db.String(128), nullable=True)
	leader_level = db.Column(db.Integer, nullable=True)
	allyboxspace = db.Column(db.Integer, nullable=True)
	level = db.Column(db.Integer, nullable=True)
	friends = db.Column(db.String(8192), nullable=True, default="[]")
	friend_requests = db.Column(db.String(8192), nullable=True, default="[]")
	last_online = db.Column(db.Integer, nullable=True, default=int(time.time()))
	helpcount = db.Column(db.Integer, nullable=True, default=0)
	anonymoushelpcount = db.Column(db.Integer, nullable=True, default=0)
	devicename = db.Column(db.String(128), nullable=True)

	name_change_count = db.Column(db.Integer, nullable=False, default=0)

	def as_dict(self):
		return {c.name: getattr(self, c.name) for c in self.__table__.columns}

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def MigratePlayerNameChangeCount():
    """Add name_change_count to Primary and enabled LocalInstance without deleting data."""
    from sqlalchemy import inspect, text

    # Primary database is managed by Flask-SQLAlchemy.
    try:
        inspector = inspect(db.engine)
        if "player" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("player")}
            if "name_change_count" not in columns:
                with db.engine.begin() as connection:
                    connection.execute(text(
                        "ALTER TABLE player ADD COLUMN name_change_count INTEGER NOT NULL DEFAULT 0"
                    ))
                print("[Player Migration] Added 'name_change_count' to Primary.")
    except Exception as exc:
        app.logger.exception("[Player Migration] Primary migration failed: %s", exc)

    # Player ORM queries can be routed to LocalInstance, so that database needs
    # the same schema even though db.create_all() only creates Primary tables.
    local_path = _local_instance_db_path()
    if not local_path or not os.path.isfile(local_path):
        return

    connection = None
    try:
        connection = sqlite3.connect(local_path, timeout=15)
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "player" not in tables:
            return
        columns = {row[1] for row in connection.execute("PRAGMA table_info(player)")}
        if "name_change_count" not in columns:
            connection.execute(
                "ALTER TABLE player ADD COLUMN name_change_count INTEGER NOT NULL DEFAULT 0"
            )
            connection.commit()
            print("[Player Migration] Added 'name_change_count' to LocalInstance.")
    except Exception as exc:
        app.logger.exception("[Player Migration] LocalInstance migration failed: %s", exc)
    finally:
        if connection is not None:
            connection.close()


def _loads_legacy_player_json(text):
    """Parse player JSON while tolerating legacy trailing commas outside strings."""
    cleaned = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        if in_string:
            cleaned.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            cleaned.append(char)
            index += 1
            continue

        if char == ",":
            lookahead = index + 1
            while lookahead < length and text[lookahead].isspace():
                lookahead += 1
            if lookahead < length and text[lookahead] in "]}":
                index += 1
                continue

        cleaned.append(char)
        index += 1

    return json.loads("".join(cleaned))


def _decode_game_save_for_name_change(game_text):
    """Decode the save format used by the persistence API, or accept raw JSON saves."""
    if isinstance(game_text, (bytes, bytearray)):
        game_text = game_text.decode("utf-8", errors="strict")
    if not isinstance(game_text, str) or not game_text.strip():
        raise ValueError("Empty player save")

    stripped = game_text.strip()
    if stripped.startswith("{"):
        data = _loads_legacy_player_json(stripped)
        if not isinstance(data, dict):
            raise ValueError("Player save JSON is not an object")
        return data, "json"

    # Accept both '&data=' and '?data=' / 'data=' prefixes.
    marker = stripped.find("data=")
    if marker < 0:
        raise ValueError("Player save has no data payload")
    encoded = stripped[marker + len("data="):].split("&", 1)[0]
    encoded = encoded.strip()
    if not encoded:
        raise ValueError("Player save data payload is empty")

    # The persistence layer may store URL-encoded form data. In that case,
    # '+' can already have been converted to a space before it reaches this
    # decoder. Restore Base64 characters and support both standard and URL-safe
    # Base64 while preserving the original save prefix/suffix.
    from urllib.parse import unquote
    encoded = unquote(encoded).replace(" ", "+")
    encoded = encoded.replace("-", "+").replace("_", "/")
    encoded += "=" * ((4 - len(encoded) % 4) % 4)

    try:
        compressed = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("Player save payload is not valid Base64") from exc

    try:
        payload = gzip.decompress(compressed)
    except Exception as gzip_error:
        # Use the same decoder as the rest of the server as a compatibility
        # fallback for legacy persistence payloads.
        decoded = DecryptGameData(game_text)
        if isinstance(decoded, dict):
            return decoded, ("encoded", marker, len("data="))
        raise ValueError("Player save payload is not valid GZIP data") from gzip_error

    try:
        data = _loads_legacy_player_json(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Decoded player save is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Decoded player save is not an object")
    return data, ("encoded", marker, len("data="))


def _encode_game_save_with_name(game_text, save_data, save_format="encoded"):
    """Replace only the save payload while preserving the original storage format."""
    payload = json.dumps(save_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if save_format == "json":
        return payload.decode("utf-8")

    # SQLite normally returns TEXT as str, but legacy rows may contain bytes.
    # The decoder accepts bytes too, so normalize here before searching for data=.
    if isinstance(game_text, (bytes, bytearray)):
        game_text = game_text.decode("utf-8", errors="strict")
    if not isinstance(game_text, str):
        raise TypeError("Game save must be text or UTF-8 bytes")

    marker = game_text.find("data=")
    if marker < 0:
        raise ValueError("Game save does not contain a data payload")
    prefix = game_text[:marker + len("data=")]
    compressed = gzip.compress(payload, mtime=0)
    encoded = base64.b64encode(compressed).decode("ascii")
    suffix_start = marker + len("data=")
    suffix_end = game_text.find("&", suffix_start)
    suffix = game_text[suffix_end:] if suffix_end >= 0 else ""
    return prefix + encoded + suffix


@app.route("/persist/multiplayer_name_change/", methods=["POST"])
def PersistMultiplayerNameChange():
    """
    Change MultiplayerPlayerName without replacing the player's save.
    Requires the same request headers and device-name check as game persistence.
    POST form fields: player_id, name.
    Empty names and the first transition from empty to non-empty are free;
    subsequent non-empty renames cost 4500 FreeHardCurrency.
    """
    try:
        # Match the existing game-persistence request protocol.
        username = request.headers.get("Player-Id", "").strip()
        required_headers = ("Age", "Platform", "Version")
        if not username:
            return jsonify({"success": False, "error": "MISSING_PLAYER_ID_HEADER"}), 400
        if any(not request.headers.get(header) for header in required_headers):
            return jsonify({"success": False, "error": "MISSING_REQUIRED_HEADER"}), 400
        if request.headers.get("User-Agent") != "Innertube Explorer v0.1":
            return jsonify({"success": False, "error": "INVALID_USER_AGENT"}), 400
        if not request.headers.get("X-Nick-Description"):
            return jsonify({"success": False, "error": "MISSING_DEVICE_NAME"}), 400
        if InvalidUsername(username):
            return jsonify({"success": False, "error": "INVALID_USERNAME"}), 400

        form = parse_qs(request.get_data(as_text=True), keep_blank_values=True)
        form = {key: values[0] if len(values) == 1 else values for key, values in form.items()}

        # Never allow the body to select a different account from the request identity.
        player_id = str(form.get("player_id", "") or "").strip()
        if not player_id or not hmac.compare_digest(player_id, username):
            return jsonify({"success": False, "error": "PLAYER_ID_MISMATCH"}), 403

        raw_name = form.get("name", "")
        if not isinstance(raw_name, str):
            return jsonify({"success": False, "error": "INVALID_NAME"}), 400
        new_name = raw_name.strip()
        if len(new_name) > 128:
            return jsonify({"success": False, "error": "NAME_TOO_LONG"}), 400
        if any(ord(ch) < 32 for ch in new_name):
            return jsonify({"success": False, "error": "INVALID_NAME"}), 400

        if IsUserBanned(username, IPFromRequest(request)):
            return jsonify({"success": False, "error": "USER_BANNED"}), 403

        player = Player.query.filter_by(username=username).with_for_update().first()
        if player is None:
            return jsonify({"success": False, "error": "PLAYER_NOT_FOUND"}), 404

        # Device names are not an authentication factor. Do not reject a
        # multiplayer-name change only because the device label changed.
        # Keep the stored device binding untouched; account authentication
        # must be enforced by the game's account/login flow.
        device_name = request.headers.get("X-Nick-Description", "")
        if player.devicename is None or player.devicename == b"" or player.devicename == "":
            player.devicename = device_name

        game_text = player.game
        if isinstance(game_text, bytes):
            game_text = game_text.decode("utf-8", errors="strict")
        if not isinstance(game_text, str) or not game_text.strip():
            db.session.rollback()
            return jsonify({"success": False, "error": "PLAYER_SAVE_UNAVAILABLE"}), 400

        try:
            save_data, save_format = _decode_game_save_for_name_change(game_text)
        except Exception:
            db.session.rollback()
            app.logger.exception("[multiplayer_name_change] Could not decode save for %s", username)
            return jsonify({"success": False, "error": "INVALID_PLAYER_SAVE"}), 400

        current_name = save_data.get("MultiplayerPlayerName", "")
        current_name = current_name if isinstance(current_name, str) else str(current_name or "")
        current_name = current_name.strip()
        db_name = (player.multiplayer_name or "").strip()
        count = max(0, int(player.name_change_count or 0))

        try:
            free_currency = int(save_data.get("FreeHardCurrency", 0) or 0)
        except (TypeError, ValueError):
            db.session.rollback()
            return jsonify({"success": False, "error": "INVALID_CURRENCY"}), 400

        # Return early only when BOTH the save and database already match.
        # This ensures an empty name is also written to the database when needed.
        if current_name == new_name and db_name == new_name:
            db.session.rollback()
            return jsonify({
                "success": True,
                "changed": False,
                "name": new_name,
                "cost": 0,
                "free_hard_currency": free_currency,
                "name_change_count": count
            }), 200

        # Empty names ("", "   ") are always free.
        # The first non-empty name is free.
        # Subsequent non-empty names cost 4500 gems.
        cost = 4500 if new_name and count > 0 else 0

        if free_currency < cost:
            db.session.rollback()
            return jsonify({
                "success": False,
                "error": "INSUFFICIENT_FREE_HARD_CURRENCY",
                "required": cost,
                "free_hard_currency": free_currency
            }), 400

        # Update only the requested name and fee; preserve every other save field.
        save_data["MultiplayerPlayerName"] = new_name
        if cost:
            save_data["FreeHardCurrency"] = free_currency - cost

        player.game = _encode_game_save_with_name(game_text, save_data, save_format)
        player.multiplayer_name = new_name
        if new_name:
            player.name_change_count = count + 1
        player.last_online = int(time.time())
        db.session.commit()

        Log("player", f"Player {username} changed multiplayer name (empty={not bool(new_name)}, cost={cost})")
        return jsonify({
            "success": True,
            "changed": True,
            "name": new_name,
            "cost": cost,
            "free_hard_currency": free_currency - cost,
            "name_change_count": int(player.name_change_count or 0)
        }), 200

    except Exception:
        db.session.rollback()
        app.logger.exception("[multiplayer_name_change] Unexpected server error")
        return jsonify({"success": False, "error": "INTERNAL_SERVER_ERROR"}), 500


class CalendarClaim(db.Model):

    __tablename__ = "calendar_claim"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    player_id = db.Column(
        db.String,
        nullable=False
    )

    calendar_id = db.Column(
        db.String,
        nullable=False
    )

    day = db.Column(
        db.Integer,
        nullable=False
    )

    claimed_at = db.Column(
        db.DateTime,
        nullable=False
    )

    __table_args__ = (
        db.UniqueConstraint(
            "player_id",
            "calendar_id",
            "day",
            name="uq_calendar_player_day"
        ),
    )

# =========================================================
# CALENDAR CONFIG
# =========================================================

CALENDAR_ID = "September2026"

CALENDAR_TIMEZONE = ZoneInfo(
    "Europe/Istanbul"
)

CALENDAR_START_LOCAL = datetime(
    2026,
    8,
    30,
    0,
    0,
    0,
    tzinfo=CALENDAR_TIMEZONE
)

CALENDAR_END_LOCAL = datetime(
    2026,
    9,
    23,
    23,
    59,
    59,
    tzinfo=CALENDAR_TIMEZONE
)

CALENDAR_DAYS = 25


# =========================================================
# CALENDAR STATE
# =========================================================

def GetCalendarState():

    now_utc = datetime.now(
        timezone.utc
    )

    now_local = now_utc.astimezone(
        CALENDAR_TIMEZONE
    )

    start_local = CALENDAR_START_LOCAL
    end_local = CALENDAR_END_LOCAL

    # -----------------------------------------------------
    # NOT STARTED
    # -----------------------------------------------------

    if now_local < start_local:

        return {
            "active": False,
            "started": False,
            "ended": False,
            "day": 0,

            "server_time":
                now_utc.isoformat(),

            "local_time":
                now_local.isoformat()
        }

    # -----------------------------------------------------
    # ENDED
    # -----------------------------------------------------

    if now_local > end_local:

        return {
            "active": False,
            "started": True,
            "ended": True,
            "day": CALENDAR_DAYS,

            "server_time":
                now_utc.isoformat(),

            "local_time":
                now_local.isoformat()
        }

    # -----------------------------------------------------
    # CURRENT DAY
    # -----------------------------------------------------

    day = (
        now_local.date()
        - start_local.date()
    ).days + 1

    if day < 1:
        day = 1

    if day > CALENDAR_DAYS:
        day = CALENDAR_DAYS

    return {
        "active": True,
        "started": True,
        "ended": False,
        "day": day,

        "server_time":
            now_utc.isoformat(),

        "local_time":
            now_local.isoformat()
    }

class MonthlyStoreRotation(db.Model):
    __tablename__ = "monthly_store_rotation"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    store_type = db.Column(
        db.String(32),
        nullable=False
    )

    month_key = db.Column(
        db.String(7),
        nullable=False
    )

    offer_id = db.Column(
        db.String(255),
        nullable=False
    )

    cycle = db.Column(
        db.Integer,
        nullable=False,
        default=1
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        db.UniqueConstraint(
            "store_type",
            "month_key",
            "offer_id",
            name="uq_monthly_store_offer"
        ),
    )

class MonthlyStorePurchase(db.Model):
    __tablename__ = "monthly_store_purchase"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    player_id = db.Column(
        db.String(255),
        nullable=False
    )

    store_type = db.Column(
        db.String(32),
        nullable=False
    )

    offer_id = db.Column(
        db.String(255),
        nullable=False
    )

    month_key = db.Column(
        db.String(7),
        nullable=False
    )

    cycle = db.Column(
        db.Integer,
        nullable=False
    )

    buy_count = db.Column(
        db.Integer,
        nullable=False,
        default=0
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc)
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        db.UniqueConstraint(
            "player_id",
            "store_type",
            "offer_id",
            "month_key",
            "cycle",
            name="uq_monthly_store_purchase"
        ),
    )

@app.route("/")
def Index():
	return "200 App server running"

@app.route("/persist/static/manifest.json")
def Manifest():
	with open("data/persist/manifest.json", "r") as f:
		return f.read()

@app.route("/persist/static/<string:digest>", methods=["GET"])
def PersistStaticContentByDigest(digest):
    """
    Serve content-patcher files by their MD5 digest.

    SQContentPatcher requests:
        /persist/static/<manifest-entry.d>

    The manifest maps each digest (d) to a logical content path (n).
    Only files explicitly listed in the manifest are eligible to be served.
    """
    # Accept only a 32-character MD5 hex digest; avoids path-based requests.
    if not re.fullmatch(r"[a-fA-F0-9]{32}", digest):
        return make_response("Not found", 404)

    manifest_path = os.path.join(app.root_path, "data", "persist", "manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
    except (OSError, json.JSONDecodeError):
        app.logger.exception("Unable to read content manifest")
        return make_response("Content manifest unavailable", 500)

    contents = manifest.get("contents", [])
    if not isinstance(contents, list):
        return make_response("Invalid content manifest", 500)

    entry = next(
        (
            item for item in contents
            if isinstance(item, dict)
            and str(item.get("d", "")).lower() == digest.lower()
            and isinstance(item.get("n"), str)
        ),
        None
    )
    if entry is None:
        return make_response("Content digest not found in manifest", 404)

    # Manifest names are logical paths relative to data/persist.
    persist_root = os.path.realpath(os.path.join(app.root_path, "data", "persist"))
    relative_name = entry["n"].replace("\\", "/").lstrip("/")
    candidate = os.path.realpath(os.path.join(persist_root, relative_name))

    # Prevent manifest paths from escaping data/persist.
    if os.path.commonpath([persist_root, candidate]) != persist_root:
        app.logger.warning("Rejected unsafe content path in manifest: %r", entry["n"])
        return make_response("Invalid content path", 400)

    if not os.path.isfile(candidate):
        return make_response("Content file not found", 404)

    # Verify the bytes against the manifest before serving.
    try:
        hasher = hashlib.md5()
        with open(candidate, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError:
        app.logger.exception("Unable to read content file: %s", candidate)
        return make_response("Content file unavailable", 500)

    if not hmac.compare_digest(hasher.hexdigest().lower(), digest.lower()):
        app.logger.error("Content MD5 mismatch for manifest entry: %s", entry["n"])
        return make_response("Content hash mismatch", 500)

    response = make_response(send_from_directory(
        os.path.dirname(candidate),
        os.path.basename(candidate),
        conditional=True
    ))
    response.headers["Content-Type"] = "application/octet-stream"
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


CARD_BACK_STORE_BLUEPRINT = "db_StoreBuyCardBacks.json"
CARD_BACK_STORE_MONTHLY_COUNT = 15


def filter_monthly_cardback_store(blueprint_data):
    """Keep Set=Permanent, rotate Set=Route, and exclude Set=Disable entries."""
    if isinstance(blueprint_data, dict):
        entries = blueprint_data.get("data", [])
    elif isinstance(blueprint_data, list):
        entries = blueprint_data
    else:
        return blueprint_data

    entries = [entry for entry in entries if isinstance(entry, dict)]

    # Disabled entries must never be sent to the client or accepted for purchase.
    enabled_entries = [
        entry for entry in entries
        if str(entry.get("Set", "")).strip().casefold() != "disable"
    ]

    permanent = [
        entry for entry in enabled_entries
        if str(entry.get("Set", "")).strip().casefold() == "permanent"
    ]
    rotating = [
        entry for entry in enabled_entries
        if str(entry.get("Set", "")).strip().casefold() == "route"
        and str(entry.get("ID", "")).strip()
        and str(entry.get("CardBackID", "")).strip()
    ]
    rotating.sort(key=lambda entry: str(entry.get("ID", "")))

    # Keep the route pool stable and advance by 15 each month.
    pool_seed = int(hashlib.sha256(
        b"cardback-store-rotation-v1"
    ).hexdigest()[:16], 16)
    random.Random(pool_seed).shuffle(rotating)

    now = datetime.now(timezone.utc)
    month_index = now.year * 12 + now.month - 1
    selected_count = min(CARD_BACK_STORE_MONTHLY_COUNT, len(rotating))
    if len(rotating) >= CARD_BACK_STORE_MONTHLY_COUNT * 2:
        start_index = (month_index * CARD_BACK_STORE_MONTHLY_COUNT) % len(rotating)
        selected = [
            rotating[(start_index + offset) % len(rotating)]
            for offset in range(selected_count)
        ]
    else:
        month_seed = int(hashlib.sha256(
            f"cardback-store:{now.year}-{now.month:02d}".encode("utf-8")
        ).hexdigest()[:16], 16)
        selected = random.Random(month_seed).sample(rotating, selected_count)

    selected.sort(key=lambda entry: str(entry.get("ID", "")))
    result = selected + permanent
    if isinstance(blueprint_data, dict):
        filtered = dict(blueprint_data)
        filtered["data"] = result
        return filtered
    return result


CREATURE_STORE_BLUEPRINT = "db_StoreBuyCreatures.json"
CREATURE_STORE_WEEKLY_COUNTS = {1: 3, 2: 2, 3: 2}


def filter_weekly_creature_store(blueprint_data):
    """Return the same deterministic seven creature offers to every player each ISO week."""
    if isinstance(blueprint_data, dict):
        entries = blueprint_data.get("data", [])
    elif isinstance(blueprint_data, list):
        entries = blueprint_data
    else:
        return blueprint_data

    if not isinstance(entries, list):
        return blueprint_data

    pool = [entry for entry in entries if isinstance(entry, dict)]
    if not pool:
        return [] if isinstance(blueprint_data, list) else {**blueprint_data, "data": []}

    creature_path = os.path.join(
        app.root_path, "data", "persist", "blueprints", "db_Creatures.json"
    )
    try:
        with open(creature_path, "r", encoding="utf-8") as stream:
            creature_rows = json.load(stream)
    except (OSError, ValueError, TypeError):
        app.logger.exception("[Creature Store] Could not read db_Creatures.json")
        # Fail closed: do not accidentally expose the entire store if rarity data is unavailable.
        return [] if isinstance(blueprint_data, list) else {**blueprint_data, "data": []}

    rarity_by_id = {}
    if isinstance(creature_rows, list):
        for creature in creature_rows:
            if not isinstance(creature, dict):
                continue
            creature_id = str(creature.get("ID", "") or "").strip()
            try:
                rarity = int(str(creature.get("Rarity", "") or "").strip())
            except (TypeError, ValueError):
                continue
            if creature_id:
                rarity_by_id[creature_id.casefold()] = rarity

    by_rarity = {rarity: [] for rarity in CREATURE_STORE_WEEKLY_COUNTS}
    for offer in pool:
        creature_id = str(offer.get("CreatureID", "") or "").strip()
        # The store is intended for base creatures only; awaken variants are not eligible.
        if not creature_id.casefold().endswith("_base"):
            continue
        rarity = rarity_by_id.get(creature_id.casefold())
        if rarity in by_rarity:
            by_rarity[rarity].append(offer)

    week_key = datetime.now(timezone.utc).strftime("%G-%V")
    selected = []
    for rarity, required_count in CREATURE_STORE_WEEKLY_COUNTS.items():
        candidates = sorted(
            by_rarity[rarity],
            key=lambda offer: (
                str(offer.get("CreatureID", "")).casefold(),
                str(offer.get("ID", "")).casefold()
            )
        )
        if len(candidates) < required_count:
            app.logger.error(
                "[Creature Store] Weekly rotation needs %s rarity-%s offers; only %s available",
                required_count, rarity, len(candidates)
            )
            return [] if isinstance(blueprint_data, list) else {**blueprint_data, "data": []}

        seed_text = "creature-store-weekly-v1:%s:rarity-%s" % (week_key, rarity)
        seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:16], 16)
        selected.extend(random.Random(seed).sample(candidates, required_count))

    selected.sort(key=lambda offer: str(offer.get("ID", "")).casefold())
    if isinstance(blueprint_data, dict):
        filtered = dict(blueprint_data)
        filtered["data"] = selected
        return filtered
    return selected


@app.route("/persist/static/blueprints/<path:filename>", methods=['GET'])
@app.route("/persist/static/Blueprints/<path:filename>", methods=['GET'])
def get_blueprints(filename):
    file_path = os.path.join("data/persist/blueprints", filename)

    with open(file_path, "r", encoding="utf-8") as file:
        content = file.read()

    basename = os.path.basename(filename).lower()
    if basename == CARD_BACK_STORE_BLUEPRINT.lower():
        content = json.dumps(
            filter_monthly_cardback_store(json.loads(content)),
            ensure_ascii=False
        )
    elif basename == CREATURE_STORE_BLUEPRINT.lower():
        content = json.dumps(
            filter_weekly_creature_store(json.loads(content)),
            ensure_ascii=False
        )

    response = make_response(content)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    # The offers are stable for a week; avoid stale CDN/browser copies across rotation.
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


#only works in v1.18.0
@app.route("/persist/static/blueprints", methods=['GET'])
def Blueprints():
    data = []
    for root, dirs, files in os.walk("data/persist/blueprints"):
        for file in files:
            with open(os.path.join(root, file), "r", encoding="utf-8") as stream:
                blueprint_data = stream.read()
            if file.lower() == CARD_BACK_STORE_BLUEPRINT.lower():
                blueprint_data = json.dumps(
                    filter_monthly_cardback_store(json.loads(blueprint_data)),
                    ensure_ascii=False
                )
            elif file.lower() == CREATURE_STORE_BLUEPRINT.lower():
                blueprint_data = json.dumps(
                    filter_weekly_creature_store(json.loads(blueprint_data)),
                    ensure_ascii=False
                )
            data.append({
                "name": file.replace(".json", ""),
                "data": blueprint_data
            })
    return jsonify(data)


def get_monthly_store_offers(
    store_type,
    blueprint_filename,
    offer_count=6
):
    current_time = datetime.now(timezone.utc)

    month_key = (
        f"{current_time.year}-"
        f"{current_time.month:02d}"
    )

    # ============================================================
    # BLUEPRINT PATH
    # ============================================================

    blueprint_path = os.path.join(
        "data",
        "persist",
        "blueprints",
        blueprint_filename
    )

    print("========================================")
    print("[Monthly Store] Blueprint Debug")
    print("store_type:", store_type)
    print("blueprint_filename:", blueprint_filename)
    print("blueprint_path:", blueprint_path)
    print("absolute_path:", os.path.abspath(blueprint_path))
    print("exists:", os.path.exists(blueprint_path))
    print("========================================")

    if not os.path.exists(blueprint_path):
        raise FileNotFoundError(
            f"Blueprint not found: {blueprint_path}"
        )

    with open(
        blueprint_path,
        "r",
        encoding="utf-8"
    ) as f:
        blueprint_data = json.load(f)

    # ============================================================
    # BLUEPRINT ENTRIES
    # ============================================================

    if isinstance(blueprint_data, list):
        entries = blueprint_data

    elif isinstance(blueprint_data, dict):
        entries = blueprint_data.get(
            "data",
            []
        )

    else:
        raise ValueError(
            f"Invalid blueprint format: {blueprint_path}"
        )

    monthly_ids = []
    special_ids = []

    # ============================================================
    # MONTHLY / SPECIAL AYIR
    # ============================================================

    for entry in entries:

        if not isinstance(entry, dict):
            continue

        offer_id = entry.get("ID")

        if not offer_id:
            continue

        raw_special = entry.get(
            "IsSpecial",
            False
        )

        if isinstance(raw_special, bool):
            is_special = raw_special

        elif isinstance(raw_special, str):
            is_special = (
                raw_special.strip().lower()
                == "true"
            )

        else:
            is_special = False

        if is_special:
            special_ids.append(
                offer_id
            )
        else:
            monthly_ids.append(
                offer_id
            )

    monthly_ids = list(
        dict.fromkeys(monthly_ids)
    )

    special_ids = list(
        dict.fromkeys(special_ids)
    )

    # ============================================================
    # EN AZ 4 MONTHLY
    # ============================================================

    if len(monthly_ids) < offer_count:

        raise ValueError(
            f"[Monthly Store] "
            f"{store_type} has only "
            f"{len(monthly_ids)} monthly offers. "
            f"Required: {offer_count}"
        )

    # ============================================================
    # BU AY SEÇİLMİŞ Mİ?
    # ============================================================

    existing = (
        MonthlyStoreRotation.query
        .filter_by(
            store_type=store_type,
            month_key=month_key
        )
        .order_by(
            MonthlyStoreRotation.id.asc()
        )
        .all()
    )

    if len(existing) >= offer_count:

        monthly_result = [
            row.offer_id
            for row in existing[:offer_count]
        ]

        return {
            "monthly": monthly_result,
            "special": special_ids
        }

    # ============================================================
    # DAHA ÖNCE KULLANILANLAR
    # ============================================================

    used_rows = (
        MonthlyStoreRotation.query
        .filter_by(
            store_type=store_type
        )
        .all()
    )

    used_ids = {
        row.offer_id
        for row in used_rows
    }

    unused_ids = [
        offer_id
        for offer_id in monthly_ids
        if offer_id not in used_ids
    ]

    # ============================================================
    # CYCLE
    # ============================================================

    if len(unused_ids) >= offer_count:

        selected_ids = random.sample(
            unused_ids,
            offer_count
        )

        cycle = (
            max(
                [row.cycle for row in used_rows],
                default=1
            )
        )

    else:

        selected_ids = random.sample(
            monthly_ids,
            offer_count
        )

        cycle = (
            max(
                [row.cycle for row in used_rows],
                default=0
            ) + 1
        )

    # ============================================================
    # DB
    # ============================================================

    for offer_id in selected_ids:

        db.session.add(
            MonthlyStoreRotation(
                store_type=store_type,
                month_key=month_key,
                offer_id=offer_id,
                cycle=cycle
            )
        )

    db.session.commit()

    print(
        f"[Monthly Store] {store_type} / {month_key}"
    )

    print(
        f"[Monthly Store] Monthly: {selected_ids}"
    )

    print(
        f"[Monthly Store] Special: {special_ids}"
    )

    return {
        "monthly": selected_ids,
        "special": special_ids
    }

MONTHLY_STORE_BUY_LIMITS = {
    "evo": 1,
    "action": 3
}

def is_monthly_store_special(
    store_type,
    offer_id
):
    """
    Blueprint'ten offer'ın IsSpecial değerini kontrol eder.
    """

    blueprint_filename = {
        "evo": "db_StoreBuyEvoMaterials.json",
        "action": "db_StoreBuyActionCards.json"
    }.get(store_type)

    if not blueprint_filename:
        return False

    blueprint_path = os.path.join(
        "data",
        "persist",
        "blueprints",
        blueprint_filename
    )

    if not os.path.exists(blueprint_path):
        print(
            "[Monthly Store Special] "
            "Blueprint not found:",
            blueprint_path
        )
        return False

    try:
        with open(
            blueprint_path,
            "r",
            encoding="utf-8"
        ) as f:
            blueprint_data = json.load(f)

        # Blueprint listesi
        if isinstance(blueprint_data, list):
            entries = blueprint_data

        # Blueprint dictionary ise
        elif isinstance(blueprint_data, dict):
            entries = blueprint_data.get(
                "data",
                []
            )

        else:
            return False

        for entry in entries:

            if not isinstance(entry, dict):
                continue

            if entry.get("ID") != offer_id:
                continue

            raw_special = entry.get(
                "IsSpecial",
                False
            )

            if isinstance(raw_special, bool):
                return raw_special

            if isinstance(raw_special, str):
                return (
                    raw_special.strip().lower()
                    == "true"
                )

            return False

    except Exception as e:

        print(
            "[Monthly Store Special] "
            "ERROR:",
            str(e)
        )

    return False

def get_monthly_store_buy_count(
    player_id,
    store_type,
    offer_id
):
    current_time = datetime.now(timezone.utc)

    month_key = (
        f"{current_time.year}-"
        f"{current_time.month:02d}"
    )

    # ============================================================
    # SPECIAL CHECK
    # ============================================================

    is_special = is_monthly_store_special(
        store_type,
        offer_id
    )

    print(
        "[Monthly Store Count] "
        "is_special:",
        is_special
    )

    # ============================================================
    # SPECIAL OFFER
    # ============================================================

    if is_special:

        purchase = (
            MonthlyStorePurchase.query
            .filter_by(
                player_id=player_id,
                store_type=store_type,
                offer_id=offer_id,
                month_key=month_key,
                cycle=0
            )
            .first()
        )

        if purchase is None:
            return 0

        return purchase.buy_count

    # ============================================================
    # NORMAL OFFER
    # ============================================================

    rotation = (
        MonthlyStoreRotation.query
        .filter_by(
            store_type=store_type,
            month_key=month_key,
            offer_id=offer_id
        )
        .order_by(
            MonthlyStoreRotation.id.desc()
        )
        .first()
    )

    if rotation is None:
        return 0

    purchase = (
        MonthlyStorePurchase.query
        .filter_by(
            player_id=player_id,
            store_type=store_type,
            offer_id=offer_id,
            month_key=month_key,
            cycle=rotation.cycle
        )
        .first()
    )

    if purchase is None:
        return 0

    return purchase.buy_count

def get_monthly_store_buy_counts(
    player_id,
    store_type,
    offer_ids
):
    result = {}

    for offer_id in offer_ids:
        result[offer_id] = get_monthly_store_buy_count(
            player_id,
            store_type,
            offer_id
        )

    return result

def increment_monthly_store_buy_count(
    player_id,
    store_type,
    offer_id
):
    current_time = datetime.now(timezone.utc)

    month_key = (
        f"{current_time.year}-"
        f"{current_time.month:02d}"
    )

    is_special = is_monthly_store_special(
        store_type,
        offer_id
    )

    print("========================================")
    print("[Monthly Store Count] INCREMENT START")
    print("[Monthly Store Count] player_id:", player_id)
    print("[Monthly Store Count] store_type:", store_type)
    print("[Monthly Store Count] offer_id:", offer_id)
    print("[Monthly Store Count] month_key:", month_key)
    print("========================================")

    # ============================================================
    # SPECIAL OFFER
    # ============================================================

    if is_special:

        print(
            "[Monthly Store Count] "
            "SPECIAL OFFER"
        )

        buy_limit = MONTHLY_STORE_BUY_LIMITS.get(
            store_type,
            1
        )

        purchase = (
            MonthlyStorePurchase.query
            .filter_by(
                player_id=player_id,
                store_type=store_type,
                offer_id=offer_id,
                month_key=month_key,
                cycle=0
            )
            .first()
        )

        print(
            "[Monthly Store Count] "
            "SPECIAL purchase:",
            purchase
        )

        if purchase is not None:

            if purchase.buy_count >= buy_limit:

                print(
                    "[Monthly Store Count] "
                    "SPECIAL BUY_LIMIT_REACHED"
                )

                return {
                    "success": False,
                    "error": "BUY_LIMIT_REACHED",
                    "buy_count": purchase.buy_count,
                    "buy_limit": buy_limit
                }

            purchase.buy_count += 1

        else:

            purchase = MonthlyStorePurchase(
                player_id=player_id,
                store_type=store_type,
                offer_id=offer_id,
                month_key=month_key,
                cycle=0,
                buy_count=1
            )

            db.session.add(purchase)

        db.session.commit()

        print(
            "[Monthly Store Count] "
            "SPECIAL COMMIT OK"
        )

        return {
            "success": True,
            "buy_count": purchase.buy_count,
            "buy_limit": buy_limit
        }

    rotation = (
        MonthlyStoreRotation.query
        .filter_by(
            store_type=store_type,
            month_key=month_key,
            offer_id=offer_id
        )
        .order_by(
            MonthlyStoreRotation.id.desc()
        )
        .first()
    )

    print(
        "[Monthly Store Count] rotation:",
        rotation
    )

    if rotation is None:
        print(
            "[Monthly Store Count] ERROR: "
            "ROTATION NOT FOUND"
        )

        return {
            "success": False,
            "error": "OFFER_NOT_AVAILABLE"
        }

    print(
        "[Monthly Store Count] rotation.id:",
        rotation.id
    )

    print(
        "[Monthly Store Count] rotation.cycle:",
        rotation.cycle
    )

    buy_limit = MONTHLY_STORE_BUY_LIMITS.get(
        store_type,
        1
    )

    print(
        "[Monthly Store Count] buy_limit:",
        buy_limit
    )

    purchase = (
        MonthlyStorePurchase.query
        .filter_by(
            player_id=player_id,
            store_type=store_type,
            offer_id=offer_id,
            month_key=month_key,
            cycle=rotation.cycle
        )
        .first()
    )

    print(
        "[Monthly Store Count] existing purchase:",
        purchase
    )

    if purchase is not None:

        print(
            "[Monthly Store Count] "
            "EXISTING buy_count:",
            purchase.buy_count
        )

        if purchase.buy_count >= buy_limit:

            print(
                "[Monthly Store Count] "
                "BUY_LIMIT_REACHED:",
                purchase.buy_count,
                "/",
                buy_limit
            )

            return {
                "success": False,
                "error": "BUY_LIMIT_REACHED",
                "buy_count": purchase.buy_count,
                "buy_limit": buy_limit
            }

        purchase.buy_count += 1

        print(
            "[Monthly Store Count] "
            "INCREMENTED TO:",
            purchase.buy_count
        )

    else:

        print(
            "[Monthly Store Count] "
            "CREATING NEW PURCHASE"
        )

        purchase = MonthlyStorePurchase(
            player_id=player_id,
            store_type=store_type,
            offer_id=offer_id,
            month_key=month_key,
            cycle=rotation.cycle,
            buy_count=1
        )

        db.session.add(purchase)

        print(
            "[Monthly Store Count] "
            "NEW buy_count: 1"
        )

    db.session.commit()

    print(
        "[Monthly Store Count] COMMIT OK"
    )

    # ============================================================
    # DB'DEN TEKRAR OKU
    # ============================================================

    verify = (
        MonthlyStorePurchase.query
        .filter_by(
            player_id=player_id,
            store_type=store_type,
            offer_id=offer_id,
            month_key=month_key,
            cycle=rotation.cycle
        )
        .first()
    )

    print(
        "[Monthly Store Count] "
        "VERIFY DB:",
        verify
    )

    if verify is not None:
        print(
            "[Monthly Store Count] "
            "VERIFY buy_count:",
            verify.buy_count
        )

    print("========================================")
    print("[Monthly Store Count] INCREMENT END")
    print("========================================")

    return {
        "success": True,
        "buy_count": purchase.buy_count,
        "buy_limit": buy_limit
    }

@app.route(
    "/persist/static/monthly_store",
    methods=["GET"]
)
def GetMonthlyStore():

    try:

        # ============================================================
        # PLAYER
        # ============================================================

        player_id = request.args.get("player_id")

        if player_id is None:
            return jsonify({
                "success": False,
                "error": "MISSING_PLAYER_ID"
            }), 400

        # ============================================================
        # GET ROTATION
        # ============================================================

        evo = get_monthly_store_offers(
            "evo",
            "db_StoreBuyEvoMaterials.json",
            4
        )

        action = get_monthly_store_offers(
            "action",
            "db_StoreBuyActionCards.json",
            6
        )

        # ============================================================
        # BUY COUNTS
        # ============================================================

        action_offer_ids = list(
            dict.fromkeys(
                action["monthly"] +
                action["special"]
            )
        )

        evo_offer_ids = list(
            dict.fromkeys(
                evo["monthly"] +
                evo["special"]
            )
        )

        action_buy_counts = (
            get_monthly_store_buy_counts(
                player_id,
                "action",
                action_offer_ids
            )
        )

        evo_buy_counts = (
            get_monthly_store_buy_counts(
                player_id,
                "evo",
                evo_offer_ids
            )
        )

        # ============================================================
        # RESPONSE
        # ============================================================

        return jsonify({
            "success": True,

            "action_cards":
                action["monthly"],

            "action_card_special":
                action["special"],

            "evo_materials":
                evo["monthly"],

            "evo_special":
                evo["special"],

            "buy_counts": {
                "action":
                    action_buy_counts,

                "evo":
                    evo_buy_counts
            },

            "buy_limits": {
                "action":
                    MONTHLY_STORE_BUY_LIMITS["action"],

                "evo":
                    MONTHLY_STORE_BUY_LIMITS["evo"]
            }
        })

    except Exception as e:

        db.session.rollback()

        print("========================================")
        print("[Monthly Store] ERROR")
        print("Type:", type(e).__name__)
        print("Error:", str(e))
        traceback.print_exc()
        print("========================================")

        return jsonify({
            "success": False,
            "error": "SERVER_ERROR",
            "message": str(e)
        }), 500

@app.route(
    "/persist/static/monthly_store/buy",
    methods=["POST"]
)
def BuyMonthlyStore():
    """Validate a monthly offer, charge the authoritative save, then record the purchase."""
    try:
        data = request.form.to_dict()
        if not data:
            data = request.get_json(silent=True) or {}

        player_id = str(data.get("player_id", "") or "").strip()
        store_type = str(data.get("store_type", "") or "").strip().lower()
        offer_id = str(data.get("offer_id", "") or "").strip()

        app.logger.info(
            "[Monthly Store Buy] player_id=%s store_type=%s offer_id=%s",
            player_id, store_type, offer_id
        )

        if not player_id:
            return jsonify({"success": False, "error": "MISSING_PLAYER_ID"}), 400
        if store_type not in ("action", "evo"):
            return jsonify({"success": False, "error": "INVALID_STORE_TYPE"}), 400
        if not offer_id:
            return jsonify({"success": False, "error": "MISSING_OFFER_ID"}), 400
        if IsUserBanned(player_id, IPFromRequest(request)):
            return make_response("User is banned!", 400)

        db_user = Player.query.filter_by(username=player_id).first()
        if db_user is None:
            return jsonify({"success": False, "error": "PLAYER_NOT_FOUND"}), 404

        # Resolve the price from the server-side store catalog; never trust a client price.
        filename = (
            "db_StoreBuyActionCards.json"
            if store_type == "action"
            else "db_StoreBuyEvoMaterials.json"
        )
        catalog_path = os.path.join(
            app.root_path, "data", "persist", "blueprints", filename
        )
        try:
            with open(catalog_path, "r", encoding="utf-8") as stream:
                catalog_rows = json.load(stream)
        except (OSError, json.JSONDecodeError):
            app.logger.exception("[Monthly Store Buy] Could not read %s", filename)
            return jsonify({"success": False, "error": "STORE_CATALOG_UNAVAILABLE"}), 500

        offer = next(
            (
                row for row in catalog_rows
                if isinstance(row, dict)
                and str(row.get("ID", "") or "").strip().casefold() == offer_id.casefold()
            ),
            None
        )
        if offer is None:
            return jsonify({"success": False, "error": "OFFER_NOT_FOUND"}), 400

        try:
            price = int(float(offer.get("BuyCost", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            price = 0
        if price <= 0:
            return jsonify({"success": False, "error": "OFFER_NOT_PURCHASABLE"}), 400

        buy_limit = MONTHLY_STORE_BUY_LIMITS.get(store_type)
        if buy_limit is None:
            return jsonify({"success": False, "error": "INVALID_STORE_TYPE"}), 400

        current_count = get_monthly_store_buy_count(player_id, store_type, offer_id)
        if current_count >= buy_limit:
            return jsonify({
                "success": False,
                "error": "BUY_LIMIT_REACHED",
                "buy_count": current_count,
                "buy_limit": buy_limit
            }), 400

        # Charge Gems from the saved server balance, not the balance reported by the client.
        # The count helper commits on success, so this pending save update is committed with it.
        game_text = db_user.game
        if isinstance(game_text, bytes):
            game_text = game_text.decode("utf-8", errors="strict")
        save_data, save_format = _decode_game_save_for_name_change(game_text)
        if not isinstance(save_data, dict):
            return jsonify({"success": False, "error": "PLAYER_SAVE_UNAVAILABLE"}), 500

        server_free = int(save_data.get("FreeHardCurrency", 0) or 0)
        if server_free < price:
            return jsonify({
                "success": False,
                "error": "INSUFFICIENT_CURRENCY",
                "buy_cost": price,
                "balance": server_free
            }), 400

        save_data["FreeHardCurrency"] = server_free - price
        db_user.game = _encode_game_save_with_name(game_text, save_data, save_format)

        result = increment_monthly_store_buy_count(player_id, store_type, offer_id)
        if not result.get("success"):
            db.session.rollback()
            status = 400 if result.get("error") in (
                "BUY_LIMIT_REACHED", "OFFER_NOT_AVAILABLE"
            ) else 500
            return jsonify(result), status

        UpdateLastOnline(player_id)
        return jsonify({
            "success": True,
            "store_type": store_type,
            "offer_id": offer_id,
            "buy_count": result["buy_count"],
            "buy_limit": result["buy_limit"],
            "buy_cost": price,
            "currency": "HardCurrency",
            "level1": int(save_data.get("PaidHardCurrency", 0) or 0),
            "level2": server_free - price
        }), 200

    except (TypeError, ValueError, KeyError, UnicodeDecodeError):
        db.session.rollback()
        app.logger.exception("[Monthly Store Buy] Invalid player save or purchase data")
        return jsonify({"success": False, "error": "PLAYER_SAVE_UNAVAILABLE"}), 500
    except Exception:
        db.session.rollback()
        app.logger.exception("[Monthly Store Buy] Unexpected exception")
        return jsonify({"success": False, "error": "SERVER_ERROR"}), 500

@app.route("/calendar/info", methods=["GET"])
def CalendarInfo():

    try:
        player_id = request.args.get("player_id")

        state = GetCalendarState()

        response = {
            "success": True,

            "calendar_id": CALENDAR_ID,

            "active": state["active"],
            "started": state["started"],
            "ended": state["ended"],

            "current_day": state["day"],
            "total_days": CALENDAR_DAYS,

            "server_time": state["server_time"],
            "local_time": state["local_time"],

            "timezone": "Europe/Istanbul",

            "start": CALENDAR_START_LOCAL.isoformat(),
            "end": CALENDAR_END_LOCAL.isoformat(),

            "claimed_days": []
        }

        # ---------------------------------------------------------
        # PLAYER CLAIMS
        # ---------------------------------------------------------

        if player_id:

            player = Player.query.filter_by(
                username=player_id
            ).first()

            if player:

                claims = CalendarClaim.query.filter_by(
                    player_id=player.username,
                    calendar_id=CALENDAR_ID
                ).order_by(
                    CalendarClaim.day.asc()
                ).all()

                response["claimed_days"] = [
                    claim.day
                    for claim in claims
                ]

        # ---------------------------------------------------------
        # CAN CLAIM
        # ---------------------------------------------------------

        if (
            state["active"]
            and state["started"]
            and not state["ended"]
            and state["day"] > 0
        ):

            if player_id:

                already_claimed = CalendarClaim.query.filter_by(
                    player_id=player_id,
                    calendar_id=CALENDAR_ID,
                    day=state["day"]
                ).first()

                response["can_claim"] = (
                    already_claimed is None
                )

            else:

                response["can_claim"] = False

        else:

            response["can_claim"] = False

        return jsonify(response), 200

    except Exception as e:

        print(
            "[Calendar] INFO ERROR:",
            repr(e)
        )

        return jsonify({
            "success": False,
            "error": "SERVER_ERROR"
        }), 500

@app.route("/calendar/claim", methods=["POST"])
def CalendarClaimReward():

    try:
        payload = request.get_json(silent=True)

        if not payload:
            return jsonify({
                "success": False,
                "error": "JSON body required"
            }), 400

        player_id = payload.get("player_id")

        if not player_id:
            return jsonify({
                "success": False,
                "error": "player_id is required"
            }), 400

        # ---------------------------------------------------------
        # PLAYER
        # ---------------------------------------------------------

        player = Player.query.filter_by(
            username=player_id
        ).first()

        if player is None:
            return jsonify({
                "success": False,
                "error": "PLAYER_NOT_FOUND"
            }), 404

        # ---------------------------------------------------------
        # SERVER DATE
        # ---------------------------------------------------------

        state = GetCalendarState()

        if not state["started"]:
            return jsonify({
                "success": False,
                "error": "CALENDAR_NOT_STARTED",
                "server_time": state["server_time"]
            }), 400

        if state["ended"]:
            return jsonify({
                "success": False,
                "error": "CALENDAR_ENDED",
                "server_time": state["server_time"]
            }), 400

        current_day = state["day"]

        # ---------------------------------------------------------
        # IMPORTANT:
        # Client does NOT choose the day.
        # ---------------------------------------------------------

        # ---------------------------------------------------------
        # ALREADY CLAIMED?
        # ---------------------------------------------------------

        existing_claim = CalendarClaim.query.filter_by(
            player_id=player.username,
            calendar_id=CALENDAR_ID,
            day=current_day
        ).first()

        if existing_claim is not None:

            return jsonify({
                "success": False,
                "error": "ALREADY_CLAIMED",
                "day": current_day,
                "claimed_at": existing_claim.claimed_at.isoformat()
            }), 400

        # ---------------------------------------------------------
        # GET REWARD
        # ---------------------------------------------------------

        reward = GetCalendarReward(
            current_day
        )

        if reward is None:
            return jsonify({
                "success": False,
                "error": "REWARD_NOT_CONFIGURED",
                "day": current_day
            }), 500

        # ---------------------------------------------------------
        # GRANT REWARD
        # ---------------------------------------------------------

        result = GrantCalendarReward(
            player,
            reward
        )

        if not result["success"]:
            db.session.rollback()

            return jsonify(result), 400

        claim = CalendarClaim(
            player_id=player.username,
            calendar_id=CALENDAR_ID,
            day=current_day,
            claimed_at=datetime.now(timezone.utc)
        )

        db.session.add(claim)

        try:

            db.session.commit()

        except Exception as e:

            db.session.rollback()

            print(
                "[Calendar] CLAIM COMMIT ERROR:",
                repr(e)
            )

            return jsonify({
                "success": False,
                "error": "CLAIM_SAVE_FAILED"
            }), 500

        # ---------------------------------------------------------
        # SUCCESS
        # ---------------------------------------------------------

        return jsonify({
            "success": True,

            "calendar_id": CALENDAR_ID,

            "day": current_day,

            "reward": reward,

            "server_time": state["server_time"],

            "message": result.get(
                "message",
                "Calendar reward claimed."
            )
        }), 200

    except Exception as e:

        db.session.rollback()

        print(
            "[Calendar] ERROR:",
            repr(e)
        )

        return jsonify({
            "success": False,
            "error": "SERVER_ERROR"
        }), 500

def GrantCalendarReward(player, reward):

    reward_type = reward.get("type")
    quantity = int(reward.get("quantity", 0))
    gift_id = reward.get("gift_id", "")

    if quantity <= 0:
        return {
            "success": False,
            "error": "INVALID_REWARD"
        }

    game_field = player.game

    save_json = None

    if game_field:

        game_text = None

        if isinstance(
            game_field,
            (bytes, bytearray)
        ):
            try:
                game_text = game_field.decode(
                    "utf-8"
                )
            except Exception:
                game_text = None

        else:
            game_text = game_field

        # Raw JSON
        if (
            isinstance(game_text, str)
            and game_text.strip().startswith("{")
        ):
            try:
                save_json = json.loads(
                    game_text
                )
            except Exception:
                save_json = None

        # Encrypted save
        if save_json is None:

            try:
                decrypted = DecryptGameData(
                    game_field
                )

                if isinstance(
                    decrypted,
                    dict
                ):
                    save_json = decrypted

                elif isinstance(
                    decrypted,
                    str
                ):
                    save_json = json.loads(
                        decrypted
                    )

            except Exception:
                save_json = None

    if save_json is None:
        return {
            "success": False,
            "error": "INVALID_PLAYER_SAVE"
        }

    # ---------------------------------------------------------
    # HARD CURRENCY
    # ---------------------------------------------------------

    if reward_type == "HardCurrency":

        old_amount = int(
            save_json.get(
                "FreeHardCurrency",
                0
            ) or 0
        )

        new_amount = (
            old_amount + quantity
        )

        save_json[
            "FreeHardCurrency"
        ] = new_amount

        player.game = json.dumps(
            save_json,
            ensure_ascii=False
        )

        return {
            "success": True,
            "message": (
                f"+{quantity} Hard Currency"
            ),
            "reward": {
                "type": reward_type,
                "quantity": quantity
            }
        }

    # ---------------------------------------------------------
    # SOFT CURRENCY
    # ---------------------------------------------------------

    if reward_type == "SoftCurrency":

        old_amount = int(
            save_json.get(
                "SoftCurrency",
                0
            ) or 0
        )

        new_amount = (
            old_amount + quantity
        )

        save_json[
            "SoftCurrency"
        ] = new_amount

        player.game = json.dumps(
            save_json,
            ensure_ascii=False
        )

        return {
            "success": True,
            "message": (
                f"+{quantity} Soft Currency"
            ),
            "reward": {
                "type": reward_type,
                "quantity": quantity
            }
        }

    return {
        "success": False,
        "error": (
            "UNSUPPORTED_REWARD_TYPE"
        )
    }


# ============================================================
# GIFT CODES
# ============================================================

GIFT_CODES_PATH = "data/persist/gift_codes.json"
GIFT_CODES_LOCK = threading.Lock()


def EnsureGiftCodesFile():
    os.makedirs(
        os.path.dirname(GIFT_CODES_PATH),
        exist_ok=True
    )

    if not os.path.exists(GIFT_CODES_PATH):
        with open(
            GIFT_CODES_PATH,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                {"codes": []},
                f,
                ensure_ascii=False,
                indent=4
            )


def LoadGiftCodes():
    EnsureGiftCodesFile()

    try:
        with open(
            GIFT_CODES_PATH,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {"codes": []}

        if not isinstance(data.get("codes"), list):
            data["codes"] = []

        return data

    except Exception as e:
        Log(
            "giftcode",
            "Failed to load gift codes: " + repr(e)
        )
        return {"codes": []}


def SaveGiftCodes(data):
    EnsureGiftCodesFile()

    temporary_path = GIFT_CODES_PATH + ".tmp"

    with open(
        temporary_path,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=4
        )

    os.replace(
        temporary_path,
        GIFT_CODES_PATH
    )


def NormalizeGiftCode(code):
    """
    Match the Unity client normalization exactly:
    whitespace, '-' and '_' are ignored and the code is upper-cased.
    """
    if code is None:
        return ""

    return re.sub(
        r"[\s\-_]",
        "",
        str(code)
    ).upper()


def FindGiftCode(data, code):
    normalized = NormalizeGiftCode(code)

    for item in data.get("codes", []):
        if NormalizeGiftCode(item.get("code")) == normalized:
            return item

    return None


def ParseGiftCodeRewardList(raw_value, field_name):
    if raw_value is None or raw_value.strip() == "":
        return []

    try:
        parsed = json.loads(raw_value)
    except Exception:
        raise ValueError(
            field_name + " must contain valid JSON."
        )

    if not isinstance(parsed, list):
        raise ValueError(
            field_name + " must be a JSON array."
        )

    return parsed


def GiftCodeIsActive(gift):
    if not bool(gift.get("enabled", True)):
        return False

    now = datetime.now(timezone.utc)

    start_date = gift.get("start_date")
    end_date = gift.get("end_date")

    if start_date:
        try:
            start = datetime.fromisoformat(
                str(start_date).replace("Z", "+00:00")
            )

            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)

            if now < start:
                return False

        except ValueError:
            return False

    if end_date:
        try:
            end = datetime.fromisoformat(
                str(end_date).replace("Z", "+00:00")
            )

            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)

            if now > end:
                return False

        except ValueError:
            return False

    return True


def GrantGiftCodeReward(player, gift):
    game_field = player.game

    save_json = None

    if game_field:

        game_text = None

        if isinstance(
            game_field,
            (bytes, bytearray)
        ):
            try:
                game_text = game_field.decode("utf-8")
            except Exception:
                game_text = None

        else:
            game_text = game_field

        if (
            isinstance(game_text, str)
            and game_text.strip().startswith("{")
        ):
            try:
                save_json = json.loads(game_text)
            except Exception:
                save_json = None

        if save_json is None:
            try:
                decrypted = DecryptGameData(
                    game_field
                )

                if isinstance(decrypted, dict):
                    save_json = decrypted

                elif isinstance(decrypted, str):
                    save_json = json.loads(decrypted)

            except Exception:
                save_json = None

    if save_json is None:
        return {
            "success": False,
            "error": "INVALID_PLAYER_SAVE"
        }

    rewards = gift.get("rewards", {})

    if not isinstance(rewards, dict):
        return {
            "success": False,
            "error": "INVALID_REWARDS"
        }

    granted = {
        "soft_currency": 0,
        "free_hard_currency": 0,
        "paid_hard_currency": 0,
        "creatures": [],
        "action_cards": []
    }

    # --------------------------------------------------------
    # CURRENCIES
    # --------------------------------------------------------

    soft_currency = int(
        rewards.get("soft_currency", 0) or 0
    )

    free_hard_currency = int(
        rewards.get("free_hard_currency", 0) or 0
    )

    paid_hard_currency = int(
        rewards.get("paid_hard_currency", 0) or 0
    )

    if soft_currency < 0:
        return {
            "success": False,
            "error": "INVALID_SOFT_CURRENCY"
        }

    if free_hard_currency < 0:
        return {
            "success": False,
            "error": "INVALID_FREE_HARD_CURRENCY"
        }

    if paid_hard_currency < 0:
        return {
            "success": False,
            "error": "INVALID_PAID_HARD_CURRENCY"
        }

    save_json["SoftCurrency"] = (
        int(save_json.get("SoftCurrency", 0) or 0)
        + soft_currency
    )

    save_json["FreeHardCurrency"] = (
        int(save_json.get("FreeHardCurrency", 0) or 0)
        + free_hard_currency
    )

    save_json["PaidHardCurrency"] = (
        int(save_json.get("PaidHardCurrency", 0) or 0)
        + paid_hard_currency
    )

    granted["soft_currency"] = soft_currency
    granted["free_hard_currency"] = free_hard_currency
    granted["paid_hard_currency"] = paid_hard_currency

    # --------------------------------------------------------
    # INVENTORY
    # --------------------------------------------------------

    inventory = save_json.get("Inventory")

    if inventory is None:
        inventory = []
        save_json["Inventory"] = inventory

    if not isinstance(inventory, list):
        return {
            "success": False,
            "error": "INVALID_INVENTORY"
        }

    max_uid = 0

    for item in inventory:
        if not isinstance(item, dict):
            continue

        try:
            uid = int(item.get("UniqueID", 0))
            if uid > max_uid:
                max_uid = uid
        except Exception:
            pass

    next_uid = max_uid + 1

    # --------------------------------------------------------
    # CREATURES
    # --------------------------------------------------------

    creatures = rewards.get("creatures", [])

    if not isinstance(creatures, list):
        return {
            "success": False,
            "error": "INVALID_CREATURE_REWARDS"
        }

    for reward in creatures:

        if not isinstance(reward, dict):
            return {
                "success": False,
                "error": "INVALID_CREATURE_REWARD"
            }

        creature_id = str(
            reward.get("id", "")
        ).strip()

        if not creature_id:
            return {
                "success": False,
                "error": "CREATURE_ID_REQUIRED"
            }

        amount = int(
            reward.get("amount", 1)
        )

        star_rating = int(
            reward.get("star_rating", 1)
        )

        if amount < 1 or amount > 999:
            return {
                "success": False,
                "error": "INVALID_CREATURE_AMOUNT"
            }

        if star_rating < 1 or star_rating > 6:
            return {
                "success": False,
                "error": "INVALID_CREATURE_STAR_RATING"
            }

        # Inventory items are added by the Unity client from the
        # response (deliver="now"). Do not also append them here,
        # otherwise the same reward can be granted twice.
        granted["creatures"].append({
            "id": creature_id,
            "amount": amount,
            "star_rating": star_rating
        })

    # --------------------------------------------------------
    # ACTION CARDS / EX CARDS
    # --------------------------------------------------------

    action_cards = rewards.get(
        "action_cards",
        []
    )

    if not isinstance(action_cards, list):
        return {
            "success": False,
            "error": "INVALID_ACTION_CARD_REWARDS"
        }

    for reward in action_cards:

        if not isinstance(reward, dict):
            return {
                "success": False,
                "error": "INVALID_ACTION_CARD_REWARD"
            }

        card_id = str(
            reward.get("id", "")
        ).strip()

        if not card_id:
            return {
                "success": False,
                "error": "ACTION_CARD_ID_REQUIRED"
            }

        amount = int(
            reward.get("amount", 1)
        )

        if amount < 1 or amount > 999:
            return {
                "success": False,
                "error": "INVALID_ACTION_CARD_AMOUNT"
            }

        # Inventory items are added by the Unity client from the
        # response (deliver="now"). Do not also append them here,
        # otherwise the same reward can be granted twice.
        granted["action_cards"].append({
            "id": card_id,
            "amount": amount
        })

    player.game = json.dumps(
        save_json,
        ensure_ascii=False
    )

    return {
        "success": True,
        "reward": granted
    }


def GiftCodeAdminAllowed():
    if not current_user.is_authenticated:
        return False

    if not isAdmin(current_user):
        return False

    return int(getattr(current_user, "rank", 999)) == 0


@app.route(
    "/admin/gift-codes",
    methods=["GET"]
)
@login_required
def AdminGiftCodes():

    if not GiftCodeAdminAllowed():
        return abort(404)

    with GIFT_CODES_LOCK:
        data = LoadGiftCodes()

    codes = data.get("codes", [])

    for gift in codes:
        gift["used_count"] = len(
            gift.get("used_by", [])
        )

    return render_template(
        "admin_giftcodes.html",
        codes=codes
    )


@app.route(
    "/admin/gift-codes/create",
    methods=["GET", "POST"]
)
@login_required
def AdminGiftCodeCreate():

    if not GiftCodeAdminAllowed():
        return abort(404)

    if request.method == "GET":
        return render_template(
            "admin_giftcode_create.html"
        )

    code = NormalizeGiftCode(
        request.form.get("code", "")
    )

    if (
        not code
        or not re.fullmatch(
            r"[A-Z0-9_-]{3,64}",
            code
        )
    ):
        return make_response(
            "Invalid gift code. Use 3-64 letters, numbers, '_' or '-'.",
            400
        )

    try:
        soft_currency = int(
            request.form.get(
                "soft_currency",
                0
            ) or 0
        )

        free_hard_currency = int(
            request.form.get(
                "free_hard_currency",
                0
            ) or 0
        )

        paid_hard_currency = int(
            request.form.get(
                "paid_hard_currency",
                0
            ) or 0
        )

        max_uses = int(
            request.form.get(
                "max_uses",
                0
            ) or 0
        )

        if (
            soft_currency < 0
            or free_hard_currency < 0
            or paid_hard_currency < 0
            or max_uses < 0
        ):
            raise ValueError()

        creatures = ParseGiftCodeRewardList(
            request.form.get(
                "creatures_json",
                ""
            ),
            "Creatures JSON"
        )

        action_cards = ParseGiftCodeRewardList(
            request.form.get(
                "action_cards_json",
                ""
            ),
            "Action Cards JSON"
        )

    except ValueError as e:

        return make_response(
            str(e)
            if str(e)
            else "Reward values are invalid.",
            400
        )

    start_date = (
        request.form.get(
            "start_date",
            ""
        ).strip()
        or None
    )

    end_date = (
        request.form.get(
            "end_date",
            ""
        ).strip()
        or None
    )

    gift = {
        "code": code,
        "enabled": True,
        "subject": request.form.get(
            "subject",
            ""
        ).strip(),
        "message": request.form.get(
            "message",
            ""
        ).strip(),
        "start_date": start_date,
        "end_date": end_date,
        "max_uses": max_uses,
        "used_by": [],
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "created_by": current_user.username,
        "rewards": {
            "soft_currency": soft_currency,
            "free_hard_currency": free_hard_currency,
            "paid_hard_currency": paid_hard_currency,
            "creatures": creatures,
            "action_cards": action_cards
        }
    }

    with GIFT_CODES_LOCK:

        data = LoadGiftCodes()

        if FindGiftCode(
            data,
            code
        ) is not None:

            return make_response(
                "Gift code already exists.",
                409
            )

        data["codes"].append(gift)
        SaveGiftCodes(data)

    Log(
        "admin",
        current_user.username
        + " created gift code "
        + code
    )

    return redirect(
        "/admin/gift-codes"
    )

# ============================================================
# GIFT CODE REWARD CATALOG API
# ============================================================

@app.route(
    "/admin/gift-codes/catalog/<kind>",
    methods=["GET"]
)
@login_required
def AdminGiftCodeCatalog(kind):

    # Only Gift Code administrators can access the catalog.
    if not GiftCodeAdminAllowed():
        return abort(404)

    # --------------------------------------------------------
    # SELECT CATALOG
    # --------------------------------------------------------

    if kind == "creatures":

        catalog_path = (
            "data/persist/blueprints/"
            "db_Creatures.json"
        )

        fields = [
            "ID",
            "Name",
            "Prefab",
            "Faction",
            "Rarity"
        ]

    elif kind == "action-cards":

        catalog_path = (
            "data/persist/blueprints/"
            "db_ActionCards.json"
        )

        fields = [
            "ID",
            "Name",
            "TypeText",
            "Faction",
            "Rarity",
            "Cost"
        ]

    else:

        return jsonify({
            "success": False,
            "error": "UNKNOWN_CATALOG"
        }), 404

    # --------------------------------------------------------
    # LOAD JSON
    # --------------------------------------------------------

    try:

        with open(
            catalog_path,
            "r",
            encoding="utf-8"
        ) as f:

            raw_data = json.load(f)

    except Exception as e:

        Log(
            "giftcode",
            "Failed to load catalog "
            + catalog_path
            + ": "
            + repr(e)
        )

        return jsonify({
            "success": False,
            "error": "CATALOG_LOAD_FAILED"
        }), 500

    # --------------------------------------------------------
    # NORMALIZE CATALOG
    # --------------------------------------------------------

    if not isinstance(
        raw_data,
        list
    ):

        raw_data = []

    items = []

    for item in raw_data:

        if not isinstance(
            item,
            dict
        ):
            continue

        entry = {}

        for field in fields:

            value = item.get(
                field
            )

            if (
                value is not None
                and value != ""
            ):

                entry[field] = value

        # ID is required.
        if entry.get("ID"):

            items.append(
                entry
            )

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    return jsonify({
        "success": True,
        "items": items
    })

@app.route(
    "/admin/gift-codes/<code>/toggle",
    methods=["POST"]
)
@login_required
def AdminGiftCodeToggle(code):

    if not GiftCodeAdminAllowed():
        return abort(404)

    with GIFT_CODES_LOCK:

        data = LoadGiftCodes()
        gift = FindGiftCode(data, code)

        if gift is None:
            return make_response(
                "Gift code not found.",
                404
            )

        gift["enabled"] = not bool(
            gift.get("enabled", True)
        )

        SaveGiftCodes(data)

    Log(
        "admin",
        current_user.username
        + " toggled gift code "
        + NormalizeGiftCode(code)
    )

    return redirect(
        "/admin/gift-codes"
    )


@app.route(
    "/admin/gift-codes/<code>/delete",
    methods=["POST"]
)
@login_required
def AdminGiftCodeDelete(code):

    if not GiftCodeAdminAllowed():
        return abort(404)

    normalized = NormalizeGiftCode(code)

    with GIFT_CODES_LOCK:

        data = LoadGiftCodes()

        old_count = len(
            data["codes"]
        )

        data["codes"] = [
            gift
            for gift in data["codes"]
            if NormalizeGiftCode(
                gift.get("code")
            ) != normalized
        ]

        if len(data["codes"]) == old_count:
            return make_response(
                "Gift code not found.",
                404
            )

        SaveGiftCodes(data)

    Log(
        "admin",
        current_user.username
        + " deleted gift code "
        + normalized
    )

    return redirect(
        "/admin/gift-codes"
    )


@app.route(
    "/multiplayer/redeemcodeDW/",
    methods=["POST"]
)
def MultiplayerRedeemCode():
    """
    Server-side redeem endpoint used by the Unity client.

    All configured rewards are granted to the player's saved game on the
    server. The response contains a UI summary, while deliver="server"
    prevents duplicate creature/card inventory grants on the client.
    """

    try:
        payload = request.get_json(silent=True)

        if not isinstance(payload, dict):
            client_data = parse_qs(
                request.get_data().decode("utf-8")
            )

            payload = {
                key: value[0] if len(value) == 1 else value
                for key, value in client_data.items()
            }

        redeem_code = NormalizeGiftCode(
            payload.get(
                "redeemcode",
                payload.get("code", "")
            )
        )

        player_id = payload.get("player_id")

        if not player_id:
            player_id = request.headers.get("Player-Id")

        if not redeem_code:
            return jsonify({
                "success": False,
                "reason": "INVALID",
                "error": "REDEEM_CODE_REQUIRED"
            }), 400

        if not player_id:
            return jsonify({
                "success": False,
                "reason": "ERROR",
                "error": "PLAYER_ID_REQUIRED"
            }), 400

        player_id = str(player_id)

        if InvalidUsername(player_id):
            return jsonify({
                "success": False,
                "reason": "ERROR",
                "error": "INVALID_USERNAME"
            }), 400

        if IsUserBanned(
            player_id,
            IPFromRequest(request)
        ):
            return jsonify({
                "success": False,
                "reason": "ERROR",
                "error": "USER_BANNED"
            }), 400

        player = Player.query.filter_by(
            username=player_id
        ).first()

        if player is None:
            return jsonify({
                "success": False,
                "reason": "ERROR",
                "error": "PLAYER_NOT_FOUND"
            }), 404

        # The JSON gift-code store is protected by one process-wide lock.
        # This prevents two simultaneous requests from claiming the same
        # code/player pair in the same Flask process.
        with GIFT_CODES_LOCK:
            data = LoadGiftCodes()

            gift = FindGiftCode(
                data,
                redeem_code
            )

            if gift is None:
                return jsonify({
                    "success": False,
                    "reason": "INVALID",
                    "error": "INVALID_REDEEM_CODE"
                }), 400

            if not bool(gift.get("enabled", True)):
                return jsonify({
                    "success": False,
                    "reason": "ERROR",
                    "error": "REDEEM_CODE_DISABLED"
                }), 400

            now = datetime.now(timezone.utc)

            start_date = gift.get("start_date")
            end_date = gift.get("end_date")

            if start_date:
                try:
                    start = datetime.fromisoformat(
                        str(start_date).replace("Z", "+00:00")
                    )

                    if start.tzinfo is None:
                        start = start.replace(
                            tzinfo=timezone.utc
                        )

                    if now < start:
                        return jsonify({
                            "success": False,
                            "reason": "NOT_STARTED",
                            "error": "REDEEM_CODE_NOT_STARTED"
                        }), 400

                except ValueError:
                    return jsonify({
                        "success": False,
                        "reason": "ERROR",
                        "error": "INVALID_START_DATE"
                    }), 400

            if end_date:
                try:
                    end = datetime.fromisoformat(
                        str(end_date).replace("Z", "+00:00")
                    )

                    if end.tzinfo is None:
                        end = end.replace(
                            tzinfo=timezone.utc
                        )

                    if now > end:
                        return jsonify({
                            "success": False,
                            "reason": "EXPIRED",
                            "error": "REDEEM_CODE_EXPIRED"
                        }), 400

                except ValueError:
                    return jsonify({
                        "success": False,
                        "reason": "ERROR",
                        "error": "INVALID_END_DATE"
                    }), 400

            used_by = gift.get("used_by", [])

            if not isinstance(used_by, list):
                used_by = []

            player_id_string = str(player.username)

            if player_id_string in used_by:
                return jsonify({
                    "success": False,
                    "reason": "ALREADY_CLAIMED",
                    "error": "ALREADY_REDEEMED"
                }), 400

            max_uses = int(
                gift.get("max_uses", 0) or 0
            )

            if max_uses > 0 and len(used_by) >= max_uses:
                return jsonify({
                    "success": False,
                    "reason": "LIMIT_REACHED",
                    "error": "REDEEM_CODE_LIMIT_REACHED"
                }), 400

            rewards_config = gift.get("rewards", {})

            if not isinstance(rewards_config, dict):
                return jsonify({
                    "success": False,
                    "reason": "ERROR",
                    "error": "INVALID_REWARDS"
                }), 400

            try:
                coins = max(
                    0,
                    int(rewards_config.get(
                        "soft_currency",
                        0
                    ) or 0)
                )

                gems = max(
                    0,
                    int(rewards_config.get(
                        "free_hard_currency",
                        0
                    ) or 0)
                )

                paid_hard_currency = int(
                    rewards_config.get(
                        "paid_hard_currency",
                        0
                    ) or 0
                )

            except (TypeError, ValueError):
                return jsonify({
                    "success": False,
                    "reason": "ERROR",
                    "error": "INVALID_CURRENCY_REWARD"
                }), 400


            creature_rewards = rewards_config.get(
                "creatures",
                []
            )

            card_rewards = rewards_config.get(
                "action_cards",
                []
            )

            if not isinstance(creature_rewards, list):
                return jsonify({
                    "success": False,
                    "reason": "ERROR",
                    "error": "INVALID_CREATURE_REWARDS"
                }), 400

            if not isinstance(card_rewards, list):
                return jsonify({
                    "success": False,
                    "reason": "ERROR",
                    "error": "INVALID_ACTION_CARD_REWARDS"
                }), 400

            # The current Unity client expects arrays of STRING IDs.
            # Amount is represented by repeating the same ID.
            creatures = []
            for reward in creature_rewards:
                if isinstance(reward, dict):
                    item_id = str(
                        reward.get("id", "")
                    ).strip()
                    amount = int(
                        reward.get("amount", 1) or 1
                    )
                else:
                    item_id = str(reward).strip()
                    amount = 1

                if not item_id:
                    return jsonify({
                        "success": False,
                        "reason": "ERROR",
                        "error": "CREATURE_ID_REQUIRED"
                    }), 400

                if amount < 1 or amount > 999:
                    return jsonify({
                        "success": False,
                        "reason": "ERROR",
                        "error": "INVALID_CREATURE_AMOUNT"
                    }), 400

                creatures.extend([item_id] * amount)

            cards = []
            for reward in card_rewards:
                if isinstance(reward, dict):
                    item_id = str(
                        reward.get("id", "")
                    ).strip()
                    amount = int(
                        reward.get("amount", 1) or 1
                    )
                else:
                    item_id = str(reward).strip()
                    amount = 1

                if not item_id:
                    return jsonify({
                        "success": False,
                        "reason": "ERROR",
                        "error": "ACTION_CARD_ID_REQUIRED"
                    }), 400

                if amount < 1 or amount > 999:
                    return jsonify({
                        "success": False,
                        "reason": "ERROR",
                        "error": "INVALID_ACTION_CARD_AMOUNT"
                    }), 400

                cards.extend([item_id] * amount)

            # Apply every reward to the server-side saved game. The Unity
            # client only displays the returned reward summary; setting
            # deliver to "server" below prevents duplicate creature/card grants.
            grant_result = GrantGiftCodeReward(player, gift)
            if not grant_result.get("success"):
                return jsonify({
                    "success": False,
                    "reason": "ERROR",
                    "error": grant_result.get("error", "REWARD_GRANT_FAILED")
                }), 400

            db.session.commit()

            used_by.append(player_id_string)
            gift["used_by"] = used_by
            SaveGiftCodes(data)

        Log(
            "redeemcode",
            player_id_string
            + " redeemed "
            + redeem_code
        )

        return jsonify({
            "success": True,
            "reason": "OK",
            "redeemcode": redeem_code,
            "subject": gift.get(
                "subject",
                ""
            ),
            "message": gift.get(
                "message",
                ""
            ),
            "fields": {
                # ApplyServerClaim expects absolute gem balances and an
                # incremental coin amount ("gs").
                "level1": int(json.loads(player.game).get("PaidHardCurrency", 0) or 0),
                "level2": int(json.loads(player.game).get("FreeHardCurrency", 0) or 0),
                "gs": coins
            },
            "rewards": {
                "gems": gems,
                "coins": coins,
                "creatures": creatures,
                "cards": cards
            },
            "deliver": "now"
        }), 200

    except Exception as e:
        db.session.rollback()

        Log(
            "redeemcode",
            "Redeem error: " + repr(e)
        )

        return jsonify({
            "success": False,
            "reason": "ERROR",
            "error": "SERVER_ERROR"
        }), 500


@app.route("/persist/static/pvp_banlist", methods=['GET'])
def GetPVPBanlist():
	"""
	Returns the current PVP banlist based on SERVER time.

	Returns JSON:
	{
		"banned_creatures": ["CreatureID1", "CreatureID2", ...],
		"banned_leaders": ["LeaderID1", "LeaderID2", ...],
		"server_time": "2025-03-15T14:30:00Z",
		"active_bans": [...]
	}
	"""
	try:
		# Read banlist blueprint
		banlist_path = "data/persist/blueprints/db_PVPBanlist.json"

		if not os.path.exists(banlist_path):
			# If file doesn't exist yet, return empty bans
			return jsonify({
				"banned_creatures": [],
				"banned_leaders": [],
				"server_time": datetime.now(timezone.utc).isoformat(),
				"active_bans": [],
				"info": "Banlist file not found - no bans active"
			})

		with open(banlist_path, "r", encoding='utf-8') as f:
			banlist_data = json.load(f)

		# Get current SERVER time (UTC) - this can't be faked by clients!
		current_time = datetime.now(timezone.utc)

		# Collect all active bans
		banned_creatures = []
		banned_leaders = []
		active_bans_info = []

		for season in banlist_data.get("seasons", []):
			try:
				# Parse start and end dates
				start_date_str = season.get("start_date", "")
				end_date_str = season.get("end_date", "")

				# Handle ISO 8601 format with Z suffix
				start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
				end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))

				# Check if this ban period is currently active
				if start_date <= current_time <= end_date:
					# Add creatures from this active ban period
					for creature_id in season.get("banned_creatures", []):
						if creature_id not in banned_creatures:
							banned_creatures.append(creature_id)

					# Add leaders from this active ban period
					for leader_id in season.get("banned_leaders", []):
						if leader_id not in banned_leaders:
							banned_leaders.append(leader_id)

					# Track which bans are active
					active_bans_info.append({
						"id": season.get("id", "unknown"),
						"name": season.get("name", "Unnamed Ban Period"),
						"starts": start_date_str,
						"expires": end_date_str
					})

			except (KeyError, ValueError) as e:
				# Skip malformed ban entries
				print(f"[PVP Banlist] Warning: Skipping malformed ban entry: {e}")
				continue

		# Return combined banlist
		return jsonify({
			"banned_creatures": banned_creatures,
			"banned_leaders": banned_leaders,
			"server_time": current_time.isoformat(),
			"active_bans": active_bans_info
		})

	except Exception as e:
		# Log error but return empty bans to not break PVP
		print(f"[PVP Banlist] Error loading banlist: {e}")
		return jsonify({
			"banned_creatures": [],
			"banned_leaders": [],
			"server_time": datetime.now(timezone.utc).isoformat(),
			"active_bans": [],
			"error": str(e)
		})

@app.route("/persist/messages_received_ids")
def PersistMessagesReceivedIDs():
	return send_from_directory(directory="", path="data/persist/messages_received_ids.json", as_attachment=True, download_name="messages_received_ids.json")

@app.route("/persist/messages_get/<string:message>")
def PersistMessagesGet(message):
    #check if message exists
	if not os.path.exists(f"data/persist/messages/{message}.json"):
		return make_response("Message not found!", 404)
	return send_from_directory(directory="", path=f"data/persist/messages/{message}.json", as_attachment=True, download_name=f"{message}.json")

@app.route("/time/")
def Time():
    data = {
        "data": {
            "server_time": datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT'),
        }
    }
    return jsonify(data)


# ============================================================
# Google account linking / sign-in
#
# Requires the explicit SQL migration:
#   migrations/20261009_google_accounts.sql
#
# Environment:
#   GOOGLE_CLIENT_ID
#   GOOGLE_CLIENT_SECRET
#   GOOGLE_REDIRECT_URI  (must exactly match the Google OAuth client)
# ============================================================

from urllib.request import Request as _GoogleRequest, urlopen as _google_urlopen
from urllib.error import URLError as _GoogleURLError, HTTPError as _GoogleHTTPError
from urllib.parse import urlencode as _google_urlencode
from sqlalchemy import text as _google_sql_text

_GOOGLE_TICKET_TTL = 300
_GOOGLE_ALLOWED_MODES = {"link", "relink", "signin", "restore"}

def _google_config():
    """
    Read OAuth settings from PythonAnywhere environment variables first,
    then fall back to the private secret.py module used by this deployment.
    The redirect URI is fixed to this app's public callback if not configured.
    """
    client_id = (
        os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        or str(getattr(_secret_module, "GOOGLE_CLIENT_ID", "") or "").strip()
    )
    client_secret = (
        os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        or str(getattr(_secret_module, "GOOGLE_CLIENT_SECRET", "") or "").strip()
    )
    redirect_uri = (
        os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
        or str(getattr(_secret_module, "GOOGLE_REDIRECT_URI", "") or "").strip()
        or "https://cardwarskingdomepicdarkmagic.pythonanywhere.com/account/google/callback"
    )
    return client_id, client_secret, redirect_uri

def _google_json_body():
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}

def _google_ticket_hash(ticket):
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()

def _ensure_google_account_tables():
    """
    Create the Google OAuth tables in the existing Flask-SQLAlchemy database.
    This is intentionally idempotent and does not create or switch databases.
    """
    statements = (
        """
        CREATE TABLE IF NOT EXISTS google_account_links (
            google_sub TEXT NOT NULL PRIMARY KEY,
            player_username VARCHAR(80) NOT NULL UNIQUE,
            email VARCHAR(320) NOT NULL DEFAULT '',
            linked_at INTEGER NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_google_account_links_player
            ON google_account_links (player_username)
        """,
        """
        CREATE TABLE IF NOT EXISTS google_account_tickets (
            ticket_hash VARCHAR(64) NOT NULL PRIMARY KEY,
            mode VARCHAR(16) NOT NULL,
            player_username VARCHAR(80),
            oauth_state VARCHAR(128) NOT NULL UNIQUE,
            status VARCHAR(24) NOT NULL DEFAULT 'pending',
            result_username VARCHAR(80),
            email VARCHAR(320),
            google_sub TEXT,
            error VARCHAR(240),
            can_create INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_google_account_tickets_expiry
            ON google_account_tickets (expires_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_google_account_tickets_state
            ON google_account_tickets (oauth_state)
        """,
    )

    try:
        with db.engine.begin() as connection:
            for statement in statements:
                connection.execute(_google_sql_text(statement))
        app.logger.info("Google account tables are ready in the configured database.")
        return True
    except Exception:
        app.logger.exception("Could not create Google account tables in the configured database.")
        return False


def _google_db_ready():
    try:
        with db.engine.connect() as connection:
            connection.execute(_google_sql_text("SELECT 1 FROM google_account_links LIMIT 1"))
            connection.execute(_google_sql_text("SELECT 1 FROM google_account_tickets LIMIT 1"))
        return True
    except Exception:
        app.logger.exception("Google account tables are unavailable in the configured database.")
        return False

def _google_player_header():
    username = (request.headers.get("Player-Id") or "").strip()
    if not username or len(username) > 80:
        return None
    return username

def _google_ticket_row(connection, ticket):
    return connection.execute(
        _google_sql_text("""
            SELECT ticket_hash, mode, player_username, oauth_state, status,
                   result_username, email, google_sub, error, can_create,
                   expires_at, consumed
            FROM google_account_tickets
            WHERE ticket_hash = :ticket_hash
        """),
        {"ticket_hash": _google_ticket_hash(ticket)}
    ).mappings().first()

def _google_set_ticket_error(ticket_hash, message):
    with db.engine.begin() as connection:
        connection.execute(
            _google_sql_text("""
                UPDATE google_account_tickets
                SET status = 'error', error = :error, can_create = 0
                WHERE ticket_hash = :ticket_hash AND consumed = 0
            """),
            {"error": message[:240], "ticket_hash": ticket_hash}
        )

def _google_exchange_code(code):
    client_id, client_secret, redirect_uri = _google_config()
    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError("Google OAuth server configuration is incomplete.")
    form = _google_urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    token_request = _GoogleRequest(
        "https://oauth2.googleapis.com/token",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with _google_urlopen(token_request, timeout=12) as response:
        token_payload = json.loads(response.read().decode("utf-8"))
    id_token = token_payload.get("id_token")
    if not id_token:
        raise RuntimeError("Google did not return an ID token.")

    # Google validates the signature and token claims on this endpoint.
    verify_url = "https://oauth2.googleapis.com/tokeninfo?" + _google_urlencode({"id_token": id_token})
    verify_request = _GoogleRequest(verify_url, headers={"Accept": "application/json"})
    with _google_urlopen(verify_request, timeout=12) as response:
        identity = json.loads(response.read().decode("utf-8"))

    if identity.get("aud") != client_id:
        raise RuntimeError("Google token audience did not match this game.")
    if identity.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise RuntimeError("Google token issuer was invalid.")
    if not identity.get("sub"):
        raise RuntimeError("Google identity did not include a stable subject.")
    if str(identity.get("email_verified", "")).lower() != "true":
        raise RuntimeError("The Google email address is not verified.")
    return {
        "sub": str(identity["sub"]),
        "email": str(identity.get("email", ""))[:320],
    }

def _google_finish_identity(ticket_hash, google_sub, email):
    now = int(time.time())
    with db.engine.begin() as connection:
        row = connection.execute(
            _google_sql_text("""
                SELECT mode, player_username, expires_at, consumed
                FROM google_account_tickets
                WHERE ticket_hash = :ticket_hash
            """),
            {"ticket_hash": ticket_hash}
        ).mappings().first()
        if not row or row["consumed"] or int(row["expires_at"]) < now:
            return False, "This Google sign-in request expired. Please try again."

        mode = row["mode"]
        player_username = row["player_username"]

        linked = connection.execute(
            _google_sql_text("""
                SELECT player_username FROM google_account_links
                WHERE google_sub = :google_sub
            """),
            {"google_sub": google_sub}
        ).mappings().first()

        if mode in ("link", "relink"):
            if not player_username:
                return False, "The game account was not identified."
            if linked and linked["player_username"] != player_username:
                return False, "This Google account is already linked to another game account."
            if not connection.execute(
                _google_sql_text("SELECT username FROM player WHERE username = :username"),
                {"username": player_username}
            ).first():
                return False, "The game account no longer exists."

            if mode == "relink":
                connection.execute(
                    _google_sql_text("DELETE FROM google_account_links WHERE player_username = :username"),
                    {"username": player_username}
                )
            connection.execute(
                _google_sql_text("""
                    INSERT INTO google_account_links (google_sub, player_username, email, linked_at)
                    VALUES (:google_sub, :username, :email, :linked_at)
                    ON CONFLICT(google_sub) DO UPDATE SET
                        player_username = excluded.player_username,
                        email = excluded.email,
                        linked_at = excluded.linked_at
                """),
                {"google_sub": google_sub, "username": player_username, "email": email, "linked_at": now}
            )
            connection.execute(
                _google_sql_text("""
                    UPDATE google_account_tickets
                    SET status = 'ok', result_username = :username, email = :email,
                        google_sub = :google_sub, error = NULL, can_create = 0
                    WHERE ticket_hash = :ticket_hash
                """),
                {"username": player_username, "email": email, "google_sub": google_sub, "ticket_hash": ticket_hash}
            )
            return True, None

        if linked:
            username = linked["player_username"]
            connection.execute(
                _google_sql_text("""
                    UPDATE google_account_tickets
                    SET status = 'ok', result_username = :username, email = :email,
                        google_sub = :google_sub, error = NULL, can_create = 0
                    WHERE ticket_hash = :ticket_hash
                """),
                {"username": username, "email": email, "google_sub": google_sub, "ticket_hash": ticket_hash}
            )
            return True, None

        connection.execute(
            _google_sql_text("""
                UPDATE google_account_tickets
                SET status = 'needs_create', email = :email, google_sub = :google_sub,
                    error = 'No game account is linked to this Google account.',
                    can_create = 1
                WHERE ticket_hash = :ticket_hash
            """),
            {"email": email, "google_sub": google_sub, "ticket_hash": ticket_hash}
        )
        return True, None

def _google_error_page(message, status=400):
    safe = (message or "Google sign-in could not be completed.")
    safe = safe.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return make_response(
        "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Card Wars Kingdom</title></head><body style='font-family:system-ui;padding:2rem'>"
        "<h2>Card Wars Kingdom</h2><p>" + safe +
        "</p><p>You can return to the game.</p></body></html>",
        status,
        {"Content-Type": "text/html; charset=utf-8"},
    )

@app.route("/account/google/begin", methods=["POST"])
def GoogleAccountBegin():
    if not _google_db_ready():
        return jsonify({"error": "Google account service is not configured. Apply the database migration first."}), 503
    body = _google_json_body()
    mode = str(body.get("mode", "")).lower().strip()
    if mode not in _GOOGLE_ALLOWED_MODES:
        return jsonify({"error": "Unsupported Google account operation."}), 400
    if mode in ("link", "relink"):
        username = _google_player_header()
        if not username:
            return jsonify({"error": "Player-Id header is required."}), 401
        if Player.query.filter_by(username=username).first() is None:
            return jsonify({"error": "Game account was not found."}), 404
        if IsUserBanned(username, IPFromRequest(request)):
            return jsonify({"error": "This game account is banned."}), 403
    else:
        username = None

    client_id, client_secret, redirect_uri = _google_config()
    missing = []
    if not client_id:
        missing.append("GOOGLE_CLIENT_ID")
    if not client_secret:
        missing.append("GOOGLE_CLIENT_SECRET")
    if not redirect_uri:
        missing.append("GOOGLE_REDIRECT_URI")
    if missing:
        app.logger.error("Google OAuth configuration missing: %s", ", ".join(missing))
        return jsonify({
            "error": "Google OAuth server configuration is incomplete.",
            "missing": missing
        }), 503

    ticket = secrets.token_urlsafe(32)
    ticket_hash = _google_ticket_hash(ticket)
    now = int(time.time())
    with db.engine.begin() as connection:
        connection.execute(
            _google_sql_text("""
                INSERT INTO google_account_tickets
                    (ticket_hash, mode, player_username, oauth_state, status,
                     created_at, expires_at, consumed, can_create)
                VALUES (:ticket_hash, :mode, :username, :oauth_state, 'pending',
                        :created_at, :expires_at, 0, 0)
            """),
            {
                "ticket_hash": ticket_hash,
                "mode": mode,
                "username": username,
                "oauth_state": secrets.token_urlsafe(32),
                "created_at": now,
                "expires_at": now + _GOOGLE_TICKET_TTL,
            }
        )
        # Return the state only through the browser URL generation, never to the Unity client.
        row = connection.execute(
            _google_sql_text("SELECT oauth_state FROM google_account_tickets WHERE ticket_hash = :ticket_hash"),
            {"ticket_hash": ticket_hash}
        ).first()
        oauth_state = row[0]

    return jsonify({"ticket": ticket, "status": "pending"})

@app.route("/account/google/start", methods=["GET"])
def GoogleAccountStart():
    ticket = (request.args.get("ticket") or "").strip()
    if not ticket or not _google_db_ready():
        return _google_error_page("This Google sign-in request is invalid or expired.")
    now = int(time.time())
    with db.engine.connect() as connection:
        row = _google_ticket_row(connection, ticket)
    if not row or row["consumed"] or int(row["expires_at"]) < now or row["status"] != "pending":
        return _google_error_page("This Google sign-in request expired. Return to the game and try again.")
    client_id, _, redirect_uri = _google_config()
    if not client_id or not redirect_uri:
        return _google_error_page("Google OAuth server configuration is incomplete.", 503)
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + _google_urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email",
        "state": row["oauth_state"],
        "prompt": "select_account",
    })
    return redirect(auth_url, code=302)

@app.route("/account/google/callback", methods=["GET"])
def GoogleAccountCallback():
    if request.args.get("error"):
        return _google_error_page("Google sign-in was cancelled or denied.")
    code = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()
    if not code or not state or not _google_db_ready():
        return _google_error_page("Google returned an invalid sign-in response.")
    with db.engine.connect() as connection:
        row = connection.execute(
            _google_sql_text("""
                SELECT ticket_hash, expires_at, consumed
                FROM google_account_tickets
                WHERE oauth_state = :state
            """),
            {"state": state}
        ).mappings().first()
    if not row or row["consumed"] or int(row["expires_at"]) < int(time.time()):
        return _google_error_page("This Google sign-in request expired. Return to the game and try again.")
    try:
        identity = _google_exchange_code(code)
        ok, error = _google_finish_identity(row["ticket_hash"], identity["sub"], identity["email"])
        if not ok:
            _google_set_ticket_error(row["ticket_hash"], error or "Google sign-in failed.")
            return _google_error_page(error or "Google sign-in failed.")
    except (_GoogleHTTPError, _GoogleURLError, TimeoutError, ValueError, RuntimeError) as exc:
        app.logger.warning("Google OAuth callback failed: %s", type(exc).__name__)
        _google_set_ticket_error(row["ticket_hash"], "Google verification failed. Please try again.")
        return _google_error_page("Google verification failed. Return to the game and try again.")
    return _google_error_page("Google verification completed. Return to Card Wars Kingdom.", 200)

@app.route("/account/google/poll", methods=["GET"])
def GoogleAccountPoll():
    ticket = (request.args.get("ticket") or "").strip()
    if not ticket or not _google_db_ready():
        return jsonify({"status": "error", "error": "Invalid or expired request.", "can_create": False}), 400
    now = int(time.time())
    with db.engine.begin() as connection:
        row = _google_ticket_row(connection, ticket)
        if not row:
            return jsonify({"status": "error", "error": "Invalid or expired request.", "can_create": False}), 404
        if int(row["expires_at"]) < now and not row["consumed"]:
            connection.execute(
                _google_sql_text("""
                    UPDATE google_account_tickets
                    SET status = 'error', error = 'This request expired.', can_create = 0
                    WHERE ticket_hash = :ticket_hash AND status = 'pending'
                """),
                {"ticket_hash": row["ticket_hash"]}
            )
            row = _google_ticket_row(connection, ticket)
    if row["status"] == "ok":
        return jsonify({
            "status": "ok",
            "username": row["result_username"],
            "email": row["email"] or "",
            "already": False,
        })
    if row["status"] == "needs_create":
        return jsonify({
            "status": "error",
            "error": row["error"] or "No game account is linked to this Google account.",
            "can_create": True,
        })
    if row["status"] == "error":
        return jsonify({"status": "error", "error": row["error"] or "Google sign-in failed.", "can_create": False})
    return jsonify({"status": "pending"})

@app.route("/account/google/create", methods=["POST"])
def GoogleAccountCreate():
    if not _google_db_ready():
        return jsonify({
            "error": "Google account service is not configured."
        }), 503

    ticket = str(_google_json_body().get("ticket", "")).strip()

    if not ticket:
        return jsonify({
            "error": "A valid Google sign-in ticket is required."
        }), 400

    now = int(time.time())
    connection = db.session.connection()
    row = _google_ticket_row(connection, ticket)

    if (
        not row
        or row["consumed"]
        or int(row["expires_at"]) < now
    ):
        return jsonify({
            "error": "This Google sign-in request expired. Start again."
        }), 400

    if row["status"] != "needs_create" or not row["google_sub"]:
        return jsonify({
            "error": "Google identity has not been verified for account creation."
        }), 400

    existing_link = connection.execute(
        _google_sql_text("""
            SELECT player_username
            FROM google_account_links
            WHERE google_sub = :sub
        """),
        {"sub": row["google_sub"]}
    ).first()

    if existing_link:
        existing_username = existing_link[0]

        connection.execute(
            _google_sql_text("""
                UPDATE google_account_tickets
                SET status = 'ok',
                    result_username = :username,
                    can_create = 0,
                    consumed = 1
                WHERE ticket_hash = :ticket_hash
            """),
            {
                "username": existing_username,
                "ticket_hash": row["ticket_hash"]
            }
        )

        db.session.commit()

        return jsonify({
            "username": existing_username
        })

    # Generate a unique username for the new player account.
    username = None

    for _ in range(8):
        candidate = "google_" + secrets.token_hex(8)

        if (
            not InvalidUsername(candidate)
            and Player.query.filter_by(username=candidate).first() is None
        ):
            username = candidate
            break

    if username is None:
        return jsonify({
            "error": "Could not allocate a game account. Please try again."
        }), 500

    player = Player(username=username)
    db.session.add(player)
    db.session.flush()

    connection.execute(
        _google_sql_text("""
            INSERT INTO google_account_links (
                google_sub,
                player_username,
                email,
                linked_at
            )
            VALUES (
                :sub,
                :username,
                :email,
                :linked_at
            )
        """),
        {
            "sub": row["google_sub"],
            "username": username,
            "email": row["email"] or "",
            "linked_at": now
        }
    )

    connection.execute(
        _google_sql_text("""
            UPDATE google_account_tickets
            SET status = 'ok',
                result_username = :username,
                error = NULL,
                can_create = 0,
                consumed = 1
            WHERE ticket_hash = :ticket_hash
        """),
        {
            "username": username,
            "ticket_hash": row["ticket_hash"]
        }
    )

    db.session.commit()

    PlayerLog(
        IPFromRequest(request),
        username,
        "Created new player through verified Google sign-in"
    )

    return jsonify({
        "username": username
    })

@app.route("/account/google/status", methods=["POST"])
def GoogleAccountStatus():
    if not _google_db_ready():
        return jsonify({"linked": False, "email": "", "reward": False, "relink": False, "error": "Google account service is not configured."}), 503
    username = _google_player_header()
    if not username:
        return jsonify({"linked": False, "email": "", "reward": False, "relink": False}), 401
    with db.engine.connect() as connection:
        row = connection.execute(
            _google_sql_text("""
                SELECT email FROM google_account_links WHERE player_username = :username
            """),
            {"username": username}
        ).first()
    return jsonify({
        "linked": row is not None,
        "email": (row[0] or "") if row else "",
        "reward": False,
        "relink": row is not None,
    })

@app.route("/account/google/unlink", methods=["POST"])
def GoogleAccountUnlink():
    if not _google_db_ready():
        return jsonify({"error": "Google account service is not configured."}), 503
    username = _google_player_header()
    if not username:
        return jsonify({"error": "Player-Id header is required."}), 401
    with db.engine.begin() as connection:
        result = connection.execute(
            _google_sql_text("DELETE FROM google_account_links WHERE player_username = :username"),
            {"username": username}
        )
    return jsonify({"ok": True, "unlinked": result.rowcount > 0})


@app.route("/account/preAuth/")
def AccountPreAuth():
	data = {
		"data": {
			"nonce": os.urandom(32).hex()
		}
	}
	return jsonify(data)

@app.route("/account/gcAuth/", methods=['POST'])
def AccountGCAuth():
	clientData = parse_qs(request.get_data().decode('utf-8'))
	clientData = {k: v[0] if len(v) == 1 else v for k, v in clientData.items()}

	if InvalidUsername(clientData["player_id"]):
		return make_response("Invalid Username!", 400)

	if IsUserBanned(clientData["player_id"], IPFromRequest(request)):
		return make_response("User is banned!", 400)

	#Create user if it doesn't exist
	db_user = Player.query.filter_by(username=clientData["player_id"]).first()

	isplayernew = False

	if db_user is None:
		db_user = Player(username=clientData["player_id"])
		db.session.add(db_user)
		db.session.commit()
		isplayernew = True
		PlayerLog(ip=IPFromRequest(request), player=clientData["player_id"], message="Created new player")


	data = {
		"data": {
			"user_id": clientData["player_id"],
			"is_new": isplayernew
		}
	}
	return data

@app.route("/persist/getcc/")
def GetCountryCode():
	data = {
		"ip": request.headers.get("X-Forwarded-For", request.remote_addr),
		"country_code": "US"
	}
	return jsonify(data)

@app.route("/multiplayer/new_player/", methods=['POST'])
def MultiplayerNewPlayer():
	# Accept form-encoded requests safely, including blank values.
	clientData = parse_qs(
		request.get_data(as_text=True),
		keep_blank_values=True
	)
	clientData = {k: v[0] if len(v) == 1 else v for k, v in clientData.items()}

	player_id = clientData.get("player_id")
	if not isinstance(player_id, str) or not player_id.strip():
		app.logger.warning(
			"[MultiplayerNewPlayer] Missing player_id; received fields: %s",
			", ".join(sorted(clientData.keys()))
		)
		return jsonify({
			"success": False,
			"error": "Missing required field: player_id"
		}), 400
	player_id = player_id.strip()

	# Validate name only when supplied. Partial edits must not crash or
	# overwrite fields that were not included in the request.
	if "name" in clientData:
		name = clientData["name"]
		if not isinstance(name, str) or not name.strip() or InvalidUsername(name):
			return make_response("Invalid username!", 400)

	db_user = Player.query.filter_by(username=player_id).first()
	if db_user is None:
		return make_response("No player found!", 404)

	field_map = {
		"name": "multiplayer_name",
		"icon": "icon",
		"deck_rank": "deck_rank",
		"landscapes": "landscapes",
		"helper_creature": "helper_creature",
		"leader": "leader",
		"leader_level": "leader_level",
		"allyboxspace": "allyboxspace",
		"level": "level",
	}
	for request_field, model_field in field_map.items():
		if request_field not in clientData:
			continue
		value = clientData[request_field]
		if not isinstance(value, str):
			return jsonify({
				"success": False,
				"error": "Invalid field value",
				"field": request_field
			}), 400
		setattr(db_user, model_field, value)

	try:
		db.session.commit()
	except Exception:
		db.session.rollback()
		app.logger.exception(
			"[MultiplayerNewPlayer] Failed to update player %s",
			player_id
		)
		return jsonify({
			"success": False,
			"error": "Failed to update player data"
		}), 500

	PlayerLog(IPFromRequest(request), player_id, "Updated player data")

	return jsonify({
		"success": True,
		"data": {
			"name": db_user.multiplayer_name or "",
			"icon": db_user.icon or "",
			"leader": db_user.leader or "",
			"level": str(db_user.leader_level or ""),
			"trophies": "0" #Unused
		}
	})

@app.route("/multiplayer/update_deck_name/", methods=['POST'])
def MultiplayerUpdateDeckName():
	try:
		clientData = parse_qs(request.get_data(as_text=True), keep_blank_values=True)
		clientData = {k: v[0] if len(v) == 1 else v for k, v in clientData.items()}

		# Validate the payload before indexing it; malformed requests must not raise KeyError.
		required_fields = (
			"name", "player_id", "deck_rank", "landscapes",
			"helper_creature", "leader", "leader_level", "allyboxspace"
		)
		missing_fields = [
			field for field in required_fields
			if field not in clientData or not isinstance(clientData[field], str)
			or not clientData[field].strip()
		]
		if missing_fields:
			app.logger.warning(
				"[update_deck_name] Rejected request: missing/invalid fields: %s",
				", ".join(missing_fields)
			)
			return jsonify({
				"success": False,
				"error": "Missing or invalid required fields",
				"fields": missing_fields
			}), 400

		if InvalidUsername(clientData["name"]):
			return make_response("Invalid username!", 400)

		player_id = clientData["player_id"].strip()
		db_user = Player.query.filter_by(username=player_id).first()
		if db_user is None:
			return make_response("No player found!", 404)

		db_user.deck_rank = clientData["deck_rank"]
		db_user.landscapes = clientData["landscapes"]
		db_user.helper_creature = clientData["helper_creature"]
		db_user.leader = clientData["leader"]
		db_user.leader_level = clientData["leader_level"]
		db_user.allyboxspace = clientData["allyboxspace"]

		db.session.commit()
		PlayerLog(IPFromRequest(request), player_id, "Updated player data")
		return jsonify({"success": True})
	except Exception:
		db.session.rollback()
		app.logger.exception("[update_deck_name] Unexpected server error")
		return jsonify({"success": False, "error": "Internal server error"}), 500

def get_hash_string(source_value, key):
	hmac_sha256 = hmac.new(key.encode('utf-8'), source_value.encode('utf-8'), hashlib.sha256)
	return hmac_sha256.hexdigest()

@app.route("/persist/user_action2/", methods=['POST'])
def UserAction2():
	try:
		clientData = parse_qs(request.get_data(as_text=True), keep_blank_values=True)
		clientData = {k: v[0] if len(v) == 1 else v for k, v in clientData.items()}
		player_id = str(clientData.get("player_id", "") or "").strip()
		if not player_id:
			return jsonify({"success": False, "error": "Missing player_id"}), 400
		if IsUserBanned(player_id, IPFromRequest(request)):
			return make_response("User is banned!", 400)
		db_user = Player.query.filter_by(username=player_id).first()
		if db_user is None:
			return make_response("No player found!", 404)
		UpdateLastOnline(player_id)

		def int_field(name, default=None):
			value = clientData.get(name, default)
			if value is None or str(value).strip() == "":
				if default is None:
					raise ValueError("Missing " + name)
				return int(default)
			return int(value)

		try:
			paid, free = int_field("pd"), int_field("fr")
			custom = int_field("cu", 0)
			paid_delta, free_delta = int_field("dp", 0), int_field("df", 0)
			custom_delta = int_field("dc", 0)
			soft_delta, pvp_delta, shard_delta = int_field("ds", 0), int_field("dv", 0), int_field("de", 0)
			misc = int_field("misc", 0)
		except (TypeError, ValueError):
			return jsonify({"success": False, "error": "Invalid currency fields"}), 400

		event_name = str(clientData.get("evt", "") or "").strip()
		ctx = str(clientData.get("ctx", "") or "").strip()
		handle_key = "5424493204pemhi3148ifmanseu4iksdf4_4" + player_id + str(misc)
		fields = {
			"level1": paid + paid_delta,
			"level2": free + free_delta,
			"level3": custom + custom_delta,
			"gs": soft_delta, "gf": free_delta, "gv": pvp_delta, "ge": shard_delta,
			"reason": "OK", "handle": get_hash_string(player_id, handle_key)
		}

		if event_name.lower() == "purchase":
			parts = ctx.split(":")
			if len(parts) != 3:
				fields["reason"] = "INVALID_PURCHASE"
			else:
				kind, item_id, count_text = parts
				kind = kind.strip().lower().replace(" ", "").replace("-", "")
				item_id = item_id.strip()
				try:
					count = int(count_text)
				except (TypeError, ValueError):
					count = 0

				catalogs = {
					"creature": ("db_StoreBuyCreatures.json", ("CreatureID",)),
					"creatures": ("db_StoreBuyCreatures.json", ("CreatureID",)),
					"actioncard": ("db_StoreBuyActionCards.json", ("CardID",)),
					"actioncards": ("db_StoreBuyActionCards.json", ("CardID",)),
					"action_card": ("db_StoreBuyActionCards.json", ("CardID",)),
					"action_cards": ("db_StoreBuyActionCards.json", ("CardID",)),
					"spell": ("db_StoreBuyActionCards.json", ("CardID",)),
					"spells": ("db_StoreBuyActionCards.json", ("CardID",)),
					"spellcard": ("db_StoreBuyActionCards.json", ("CardID",)),
					"spellcards": ("db_StoreBuyActionCards.json", ("CardID",)),
					"card": ("db_StoreBuyActionCards.json", ("CardID",)),
					"excard": ("db_StoreBuyActionCards.json", ("CardID",)),
					"cardback": ("db_StoreBuyCardBacks.json", ("CardBackID",)),
					"cardbacks": ("db_StoreBuyCardBacks.json", ("CardBackID",)),
					"evomaterial": ("db_StoreBuyEvoMaterials.json", ("EvoMaterialID",)),
					"evomaterials": ("db_StoreBuyEvoMaterials.json", ("EvoMaterialID",)),
					"evo": ("db_StoreBuyEvoMaterials.json", ("EvoMaterialID",))
				}
				item_data, price = None, 0
				purchase_currency = "hard_free"
				if kind == "inventory":
					# Inventory purchases intentionally use an empty item ID and count=1.
					if item_id or count != 1:
						fields["reason"] = "INVALID_PURCHASE"
					else:
						try:
							misc_path = os.path.join(
								app.root_path, "data", "persist", "blueprints",
								"db_MiscParams.json"
							)
							with open(misc_path, "r", encoding="utf-8") as stream:
								misc_rows = json.load(stream)
							misc_params = {
								str(row.get("Parameter", "")): row.get("Value", "1")
								for row in misc_rows if isinstance(row, dict)
							}
							price = int(float(misc_params.get("InventorySpacePurchaseCost", 1) or 1))
							inventory_increment = int(float(misc_params.get("InventorySpacePerPurchase", 1) or 1))
							inventory_max = int(float(misc_params.get("MaxInventorySpace", 1000) or 1000))
							inventory_start = int(float(misc_params.get("StartingInventorySpace", 25) or 25))
							if price <= 0 or inventory_increment <= 0 or inventory_max <= 0:
								raise ValueError("Invalid inventory purchase settings")
							item_data = {"ID": "inventory"}
							purchase_currency = "inventory"
						except (OSError, ValueError, TypeError, json.JSONDecodeError):
							app.logger.exception("[user_action2] Inventory config read failed")
							fields["reason"] = "INVENTORY_CONFIG_ERROR"
				elif not item_id or count < 1 or count > 99:
					fields["reason"] = "INVALID_PURCHASE"
				elif kind in ("leader", "hero", "skin"):
					filename, match_keys = "db_Leaders.json", ()
					path = os.path.join(app.root_path, "data", "persist", "blueprints", filename)
					try:
						with open(path, "r", encoding="utf-8") as stream:
							rows = json.load(stream)
							if filename.lower() == CREATURE_STORE_BLUEPRINT.lower():
								rows = filter_weekly_creature_store(rows)
							requested_id = item_id.strip().casefold()
							item_data = next(
								(
									row for row in rows
									if isinstance(row, dict)
									and str(row.get("ID", "") or "").strip().casefold() == requested_id
								),
								None
							)
						if item_data:
							# Skins are identified by SkinOf and must use SkinBuyCost.
							# Never allow a non-buyable skin to be purchased.
							is_skin = bool(str(item_data.get("SkinOf", "") or "").strip())
							buyable_skin = str(item_data.get("BuyableSkin", "")).strip().lower()
							if is_skin:
								if buyable_skin == "false":
									price = 0
									fields["reason"] = "SKIN_NOT_BUYABLE"
								else:
									skin_cost = item_data.get("SkinBuyCost")
									price = int(float(skin_cost)) if skin_cost is not None and str(skin_cost).strip() else 0
									# Skins use SocialCurrency/PvpCurrency.
									purchase_currency = "hard_free"
							else:
								buy_cost = item_data.get("BuyCost")
								price = int(float(buy_cost)) if buy_cost is not None and str(buy_cost).strip() else 0
						else:
							price = 0
					except (OSError, ValueError, TypeError, json.JSONDecodeError):
						app.logger.exception("[user_action2] Leader catalog read failed")
						fields["reason"] = "LEADER_CATALOG_ERROR"
					if count != 1:
						fields["reason"] = "INVALID_PURCHASE"
				elif kind == "gacha":
					filename = "db_GachaSlots.json"
					path = os.path.join(
						app.root_path,
						"data",
						"persist",
						"blueprints",
						filename
					)

					try:
						with open(path, "r", encoding="utf-8") as stream:
							rows = json.load(stream)

						item_data = next(
							(
								row for row in rows
								if isinstance(row, dict)
								and str(row.get("ID", "")).strip().lower()
								== item_id.lower()
							),
							None
						)

						if item_data is None:
							fields["reason"] = "ITEM_NOT_FOUND"
						else:
							currency_type = str(
								item_data.get("CurrencyType", "") or ""
							).strip()

							price = int(float(item_data.get("Cost", 0) or 0))

							purchase_currency = {
								"SoftCurrency": "soft",
								"SocialCurrency": "social",
								"HardCurrency": "hard"
							}.get(currency_type, "unsupported")

							if purchase_currency == "unsupported":
								fields["reason"] = "UNSUPPORTED_GACHA_CURRENCY"

					except (OSError, ValueError, TypeError, json.JSONDecodeError):
						app.logger.exception(
							"[user_action2] Gacha catalog read failed"
						)
						fields["reason"] = "GACHA_CATALOG_ERROR"
				elif kind in catalogs:
					filename, match_keys = catalogs[kind]
					path = os.path.join(app.root_path, "data", "persist", "blueprints", filename)
					try:
						with open(path, "r", encoding="utf-8") as stream:
							rows = json.load(stream)
							if filename.lower() == CREATURE_STORE_BLUEPRINT.lower():
								rows = filter_weekly_creature_store(rows)
							requested_id = item_id.strip().casefold()
							item_data = next(
								(
									row for row in rows
									if isinstance(row, dict)
									and any(
										str(row.get(key, "") or "").strip().casefold() == requested_id
										for key in ("ID",) + tuple(match_keys)
									)
								),
								None
							)
						price = int(float(item_data.get("BuyCost", 0) or 0)) if item_data else 0
						if (
							item_data is not None
							and kind in ("cardback", "cardbacks")
							and item_data.get("ID") not in {
								entry.get("ID")
								for entry in filter_monthly_cardback_store(rows)
								if isinstance(entry, dict)
							}
						):
							fields["reason"] = "CARD_BACK_NOT_ON_SALE"
					except (OSError, ValueError, TypeError, json.JSONDecodeError):
						app.logger.exception("[user_action2] Store catalog read failed: %s", filename)
				else:
					fields["reason"] = "UNSUPPORTED_PURCHASE"

				if fields["reason"] == "OK":
					if item_data is None:
						fields["reason"] = "ITEM_NOT_FOUND"
					elif price <= 0:
						fields["reason"] = "ITEM_NOT_PURCHASABLE"
					else:
						price *= count
						save_data = DecryptGameData(db_user.game)
						if not isinstance(save_data, dict) and purchase_currency != "inventory":
							fields["reason"] = "PLAYER_SAVE_UNAVAILABLE"
						else:
							if not isinstance(save_data, dict):
								save_data = {}
							if purchase_currency == "inventory":
								# Use the persisted player save, never the client-supplied fr balance.
								try:
									game_text = db_user.game
									if isinstance(game_text, bytes):
										game_text = game_text.decode("utf-8", errors="strict")
									inventory_save, save_format = _decode_game_save_for_name_change(game_text)
									server_free = int(inventory_save.get("FreeHardCurrency", 0) or 0)
									current_space = int(inventory_save.get("InventorySpace", inventory_start) or inventory_start)
									if server_free < price:
										fields["reason"] = "INSUFFICIENT_CURRENCY"
									elif current_space + inventory_increment > inventory_max:
										fields["reason"] = "INVENTORY_FULL"
									else:
										inventory_save["FreeHardCurrency"] = server_free - price
										inventory_save["InventorySpace"] = current_space + inventory_increment
										db_user.game = _encode_game_save_with_name(
											game_text, inventory_save, save_format
										)
										fields["level1"] = int(inventory_save.get("PaidHardCurrency", 0) or 0)
										fields["level2"] = server_free - price
										db.session.commit()
								except (TypeError, ValueError, KeyError, UnicodeDecodeError):
									db.session.rollback()
									app.logger.exception("[user_action2] Inventory purchase/save failed")
									fields["reason"] = "PLAYER_SAVE_UNAVAILABLE"
								except Exception:
									db.session.rollback()
									app.logger.exception("[user_action2] Inventory purchase/save failed")
									fields["reason"] = "PLAYER_SAVE_UNAVAILABLE"

							elif purchase_currency == "hard_free":
								# Validate and deduct Gems against the persisted server save.
								# Client-reported "fr" is only a request value and may be stale.
								try:
									game_text = db_user.game
									if isinstance(game_text, bytes):
										game_text = game_text.decode("utf-8", errors="strict")
									server_save, save_format = _decode_game_save_for_name_change(game_text)
									server_free = int(server_save.get("FreeHardCurrency", 0) or 0)
									if server_free < price:
										fields["reason"] = "INSUFFICIENT_CURRENCY"
									else:
										server_save["FreeHardCurrency"] = server_free - price
										db_user.game = _encode_game_save_with_name(
											game_text, server_save, save_format
										)
										fields["level1"] = int(server_save.get("PaidHardCurrency", 0) or 0)
										fields["level2"] = server_free - price
										fields["cc"] = price
										fields["cb"] = "HardCurrency"
										db.session.commit()
								except (TypeError, ValueError, KeyError, UnicodeDecodeError):
									db.session.rollback()
									app.logger.exception("[user_action2] Store purchase/save failed")
									fields["reason"] = "PLAYER_SAVE_UNAVAILABLE"
								except Exception:
									db.session.rollback()
									app.logger.exception("[user_action2] Store purchase/save failed")
									fields["reason"] = "PLAYER_SAVE_UNAVAILABLE"

							elif purchase_currency == "hard":
								# Deduct gacha HardCurrency from the authoritative save
								# and commit it, rather than only changing response fields.
								try:
									game_text = db_user.game
									if isinstance(game_text, bytes):
										game_text = game_text.decode("utf-8", errors="strict")
									server_save, save_format = _decode_game_save_for_name_change(game_text)
									server_free = int(server_save.get("FreeHardCurrency", 0) or 0)
									server_paid = int(server_save.get("PaidHardCurrency", 0) or 0)
									if server_free + server_paid < price:
										fields["reason"] = "INSUFFICIENT_CURRENCY"
									else:
										free_spent = min(server_free, price)
										paid_spent = price - free_spent
										server_save["FreeHardCurrency"] = server_free - free_spent
										server_save["PaidHardCurrency"] = server_paid - paid_spent
										db_user.game = _encode_game_save_with_name(game_text, server_save, save_format)
										fields["level2"] = server_free - free_spent
										fields["level1"] = server_paid - paid_spent
										fields["cc"] = price
										fields["cb"] = "HardCurrency"
										db.session.commit()
								except (TypeError, ValueError, KeyError, UnicodeDecodeError):
									db.session.rollback()
									app.logger.exception("[user_action2] Gacha HardCurrency save failed")
									fields["reason"] = "PLAYER_SAVE_UNAVAILABLE"
								except Exception:
									db.session.rollback()
									app.logger.exception("[user_action2] Gacha HardCurrency save failed")
									fields["reason"] = "PLAYER_SAVE_UNAVAILABLE"

							elif purchase_currency == "soft":
								# Persist SoftCurrency server-side before returning success.
								try:
									game_text = db_user.game
									if isinstance(game_text, bytes):
										game_text = game_text.decode("utf-8", errors="strict")
									server_save, save_format = _decode_game_save_for_name_change(game_text)
									server_soft = int(server_save.get("SoftCurrency", 0) or 0)
									if server_soft < price:
										fields["reason"] = "INSUFFICIENT_CURRENCY"
									else:
										server_save["SoftCurrency"] = server_soft - price
										db_user.game = _encode_game_save_with_name(game_text, server_save, save_format)
										fields["gs"] = -price
										fields["level1"] = int(server_save.get("PaidHardCurrency", 0) or 0)
										fields["level2"] = int(server_save.get("FreeHardCurrency", 0) or 0)
										db.session.commit()
								except (TypeError, ValueError, KeyError, UnicodeDecodeError):
									db.session.rollback()
									app.logger.exception("[user_action2] Gacha SoftCurrency save failed")
									fields["reason"] = "PLAYER_SAVE_UNAVAILABLE"
								except Exception:
									db.session.rollback()
									app.logger.exception("[user_action2] Gacha SoftCurrency save failed")
									fields["reason"] = "PLAYER_SAVE_UNAVAILABLE"

							elif purchase_currency == "social":
								# PvpCurrency is stored in the player's saved game data.
								try:
									game_text = db_user.game
									if isinstance(game_text, bytes):
										game_text = game_text.decode("utf-8", errors="strict")

									server_save, save_format = _decode_game_save_for_name_change(game_text)
									server_pvp = int(server_save.get("PvpCurrency", 0) or 0)

									if server_pvp < price:
										fields["reason"] = "INSUFFICIENT_CURRENCY"
									else:
										server_save["PvpCurrency"] = server_pvp - price
										db_user.game = _encode_game_save_with_name(
											game_text, server_save, save_format
										)

										# These fields are consumed by Purchase().
										fields["cc"] = price
										fields["cb"] = "PvpCurrency"
										fields["level1"] = int(
											server_save.get("PaidHardCurrency", 0) or 0
										)
										fields["level2"] = int(
											server_save.get("FreeHardCurrency", 0) or 0
										)
										db.session.commit()

								except (TypeError, ValueError, KeyError, UnicodeDecodeError):
									db.session.rollback()
									app.logger.exception(
										"[user_action2] Gacha PvpCurrency save failed"
									)
									fields["reason"] = "PLAYER_SAVE_UNAVAILABLE"
								except Exception:
									db.session.rollback()
									app.logger.exception(
										"[user_action2] Gacha PvpCurrency save failed"
									)
									fields["reason"] = "PLAYER_SAVE_UNAVAILABLE"

			return jsonify({"success": True, "data": json.dumps({"fields": fields}, separators=(",", ":"))})

		# Even an empty evt is meaningful to PlayerSaveData.User_Action().
		PlayerLog(IPFromRequest(request), player_id, "Updated player data")
		return jsonify({"success": True, "data": json.dumps({"fields": fields}, separators=(",", ":"))})
	except Exception:
		app.logger.exception("[user_action2] Unexpected server error")
		return jsonify({"success": False, "error": "Internal server error"}), 500

def InvalidUsername(username):
	username = username.lower()
	for char in badcharaters:
		if char in username:
			return True
	if username == 'ua' or username == 'guest':
		return True
	return False

def IsBanActive(ban):
	"""Return True for permanent bans or temporary bans that have not expired."""
	if ban is None:
		return False

	# expires_at=None means permanent ban.
	if ban.expires_at is None:
		return True

	return int(time.time()) < int(ban.expires_at)


def GetActiveBan(identifier):
	"""Return an active ban, automatically removing it if it has expired."""
	ban = Bans.query.filter_by(username=identifier).first()

	if ban is None:
		return None

	if IsBanActive(ban):
		return ban

	# Temporary ban expired. Remove it automatically.
	db.session.delete(ban)
	db.session.commit()
	return None


def IsUserBanned(username, ip=None):
	# Check user ban.
	if GetActiveBan(username) is not None:
		return True

	if ip is None:
		return False

	# Check IP ban.
	if GetActiveBan(ip) is not None:
		return True

	return False

@app.route("/persist/<string:id>/game", methods=['GET', 'PUT'])
def PersistGame(id):

	if request.headers.get("Player-Id") is None:
		DiscordWebhookMessage("User attempted to access game without Player-Id header. IP: " + IPFromRequest(request))
		abort(404)
	username = request.headers.get("Player-Id")

	#verify headers
	if request.headers.get("Age") is None:
		DiscordWebhookMessage(username +" attempted to access game without Age header. IP: " + IPFromRequest(request))
		abort(404)
	if request.headers.get("User-Agent") != "Innertube Explorer v0.1":
		DiscordWebhookMessage(username +" attempted to access game without User-Agent header. IP: " + IPFromRequest(request))
		abort(404)
	if request.headers.get("Platform") is None:
		DiscordWebhookMessage(username +" attempted to access game without Platform header. IP: " + IPFromRequest(request))
		abort(404)
	if request.headers.get("Version") is None:
		DiscordWebhookMessage(username +" attempted to access game without Version header. IP: " + IPFromRequest(request))
		abort(404)
	if request.method == 'PUT':
		if request.headers.get("X-Nick-Description") is None:
			DiscordWebhookMessage(username +" attempted to access game without X-Nick-Description header. IP: " + IPFromRequest(request))
			abort(404)

	if InvalidUsername(username):
		return make_response("Invalid Username!", 400)
	if IsUserBanned(username, IPFromRequest(request)):
		return make_response("No game found!", 404)

	#Device name check
	if request.method == 'PUT':
		DeviceNameUser = Player.query.filter_by(username=username).first()
		if DeviceNameUser is None:
			return make_response("No game found!", 404)

		devicename = str(request.headers.get("X-Nick-Description", "") or "").strip()

		# Device name is optional descriptive metadata, not an account credential.
		# Empty/changing device labels must not block saving the player's game.
		if devicename:
			stored_devicename = DeviceNameUser.devicename
			if isinstance(stored_devicename, bytes):
				stored_devicename = stored_devicename.decode("utf-8", errors="replace")
			stored_devicename = str(stored_devicename or "").strip()
			if stored_devicename != devicename:
				DeviceNameUser.devicename = devicename
				db.session.commit()

	UpdateLastOnline(username)

	# A PVP restriction stored inside the player's save must not block
	# ordinary game persistence or automatically create an account-wide ban.
	# Ranked/Extreme matchmaking should enforce PVP restrictions separately.

	if request.method == 'GET':
		db_user = Player.query.filter_by(username=username).first()
		if db_user is None:
			return make_response("No game found!", 404)
		if db_user.game is None:
			return make_response("No game found!", 404)
		if db_user.game == b"" or db_user.game == b" ":
			return make_response("No game found!", 404)

		# Apply the configured inventory cleanup when the player opens the game.
		# The helper only removes the listed creature IDs and preserves other save data.
		try:
			updated_game, removed_count = remove_game_items(
				db_user.game,
				_decode_game_save_for_name_change,
				_encode_game_save_with_name
			)
			if removed_count > 0:
				db_user.game = updated_game
				db.session.commit()
				app.logger.info(
					"[RemoveGameItems] Removed %s creature item(s) for player %s",
					removed_count, username
				)
		except Exception:
			# Never prevent a player from loading the game because cleanup failed.
			db.session.rollback()
			app.logger.exception("[RemoveGameItems] Cleanup failed for player %s", username)

		return db_user.game

	if request.method == 'PUT':
		data = request.data

		#check if data is encrypted
		if not data.startswith(b"username=") or data.startswith(b"{"):
			DiscordWebhookMessage(username +" attempted to put game data without encryption. IP: " + IPFromRequest(request) + ". Data: " + data.decode("utf-8")[:50])

		db_user = Player.query.filter_by(username=username).first()
		if db_user is None:
			return make_response("No game found!", 404)
		db_user.game = data
		db.session.commit()
		return make_response("OK", 200)


def UpdateLastOnline(player_id):
	user = Player.query.filter_by(username=player_id).first()
	if user is None:
		return None
	user.last_online = int(time.time())
	db.session.commit()

def AllyBoxSpaceNotExceeded(player_id):
	user = Player.query.filter_by(username=player_id).first()
	if user is None:
		return None

	#count number of friends
	friends = json.loads(user.friends)

	friends_count = 0

	for friend in friends:
		#check if user is banned
		if IsUserBanned(friend):
			continue
		friend_user = Player.query.filter_by(username=friend).first()
		if friend_user is None:
			continue

		friends_count += 1

	return friends_count < user.allyboxspace

@app.route("/persist/friends/<string:player_id>")
def PersistFriends(player_id):
	UpdateLastOnline(player_id)
	db_user = Player.query.filter_by(username=player_id).first()

	if db_user is None:
		return make_response("No player found!", 404)

	data = []

	player_friends = json.loads(db_user.friends)

	for friend in player_friends:
		#check if user is banned
		if IsUserBanned(friend):
			continue
		friend_user = Player.query.filter_by(username=friend).first()
		allyinfo = GetAllyInfo(friend, True)
		if allyinfo is not None:
			data.append(allyinfo)

	return jsonify(data)

@app.route("/persist/friends_find_candidatesDW/", methods=['POST'])
def PersistFriendsFindCandidates():
	clientData = parse_qs(request.get_data().decode('utf-8'))
	clientData = {k: v[0] if len(v) == 1 else v for k, v in clientData.items()}

	db_user = Player.query.filter_by(username=clientData["player_id"]).first()

	if db_user is None:
		return make_response("No player found!", 404)

	data = []

	player_friends = json.loads(db_user.friends)

	for friend in player_friends:
		#check if user is banned
		if IsUserBanned(friend):
			continue
		friend_user = Player.query.filter_by(username=friend).first()
		#add ally if the level is withen clientData["level"]
		allyinfo = GetAllyInfo(friend, True)
		if allyinfo is not None:
			data.append(allyinfo)

	# Add explorers only when the requesting player's leader level is available.
	# Some legacy player records have NULL leader_level values.
	strangers = []
	if db_user.leader_level is not None:
		try:
			level_range = max(0, int(clientData.get("level", 0)))
		except (TypeError, ValueError):
			level_range = 0

		strangers = Player.query.filter(
			Player.username != clientData["player_id"],  # not the player
			Player.username.notin_(player_friends),  # not a friend
			Player.helper_creature.isnot(None),  # has a helper creature
			Player.leader_level.isnot(None),
			Player.leader_level.between(
				db_user.leader_level - level_range,
				db_user.leader_level + level_range
			)
		).order_by(func.random()).limit(3).all()

	for stranger in strangers:
		if IsUserBanned(stranger.username):
			continue

		allyinfo = GetAllyInfo(stranger.username, False)
		if allyinfo is not None:
			data.append(allyinfo)

	#randomize list
	data = random.sample(data, len(data))

	data2 = {
		"success": True,
		"data": json.dumps(data)
	}
	return jsonify(data2)

@app.route("/persist/friends_use_friendDW/", methods=['POST'])
def PersistFriendsUseFriend():
	clientData = parse_qs(request.get_data().decode('utf-8'))
	clientData = {k: v[0] if len(v) == 1 else v for k, v in clientData.items()}


	db_ally = Player.query.filter_by(username=clientData["friendid"]).first()

	if db_ally is None:
		return make_response("No player found!", 500)

	db_ally.helpcount = int(db_ally.helpcount) + 1
	db.session.commit()

	data = {
		"success": True,
	}
	return jsonify(data)

@app.route("/persist/friends_use_playerDW/", methods=['POST'])
def PersistFriendsUsePlayer():
	clientData = parse_qs(request.get_data().decode('utf-8'))
	clientData = {k: v[0] if len(v) == 1 else v for k, v in clientData.items()}


	db_stranger = Player.query.filter_by(username=clientData["userid"]).first()
	if db_stranger is None:
		return make_response("No player found!", 404)

	db_stranger.anonymoushelpcount = int(db_stranger.anonymoushelpcount) + 1
	db.session.commit()

	data = {
		"success": True,
	}
	return jsonify(data)

@app.route("/persist/friends_request_withmyinfoDW/", methods=['POST'])
def PersistFriendsRequestWithMyInfo():
	clientData = parse_qs(request.get_data().decode('utf-8'))
	clientData = {k: v[0] if len(v) == 1 else v for k, v in clientData.items()}

	UpdateLastOnline(clientData["player_id"])

	try:
		invite_user = Player.query.filter_by(username=clientData["invite_id"].replace("_", "-")).first()
	except:
		return make_response("No player found!", 400)
	if invite_user is None:
		return make_response("No player found!", 400)

	db_user = Player.query.filter_by(username=clientData["player_id"]).first()
	if db_user is None:
		return make_response("No player found!", 400)


	inviteuserfr = json.loads(invite_user.friend_requests)

	#Player ally check
	allycheck = AllyBoxSpaceNotExceeded(clientData["player_id"])
	if allycheck == False:
		return jsonify({
			"success": True,
			"info": "exceed me"
		})

	#friend ally check
	friendallycheck = AllyBoxSpaceNotExceeded(clientData["invite_id"].replace("_", "-"))
	if friendallycheck == False:
		return jsonify({
			"success": True,
			"info": "exceed"
		})

	#check if player already sent a request
	if clientData["player_id"] not in invite_user.friend_requests:
		inviteuserfr.append(clientData["player_id"])
		invite_user.friend_requests = json.dumps(inviteuserfr)
		db.session.commit()
		return jsonify({
			"success": True
		})
	else:
		return jsonify({
			"success": True,
			"info": "duplicate"
		})

def GetAllyInfo(player_id: str, isally: bool):
	db_user = Player.query.filter_by(username=player_id).first()
	if db_user is None:
		return None
	if db_user.multiplayer_name is None:
		return None
	data = {
		"fields": {
			"user_id": db_user.username,
			"name": db_user.multiplayer_name,
			"icon": db_user.icon,
			"rankxp": db_user.leader_level,
			"helpcount": db_user.helpcount if db_user.helpcount is not None else "0",
			"anonymoushelpcount": db_user.anonymoushelpcount if db_user.anonymoushelpcount is not None else "0",
			"helpercreatureid": db_user.leader,
			"helpercreature": db_user.helper_creature,
			"landscapes": db_user.landscapes,
			"ally": "1" if isally else "0",
			"sincelastactivedate": str(int(time.time()) - db_user.last_online)
		}
	}
	return data

@app.route("/persist/friends_all_requests_received/<string:player_id>", methods=['GET'])
def PersistFriendsAllRequestsReceived(player_id):
	db_user = Player.query.filter_by(username=player_id).first()
	if db_user is None:
		return make_response("No player found!", 400)

	data = []

	playerfriendrequests = json.loads(db_user.friend_requests)

	for friendrequest in playerfriendrequests:
		allyinfo = GetAllyInfo(friendrequest, False)
		if allyinfo is not None:
			data.append(allyinfo)

	return jsonify(data)

@app.route("/persist/friends_deny_request/<string:player_id>/<string:invite_id>", methods=['GET'])
def PersistFriendsDenyRequest(player_id, invite_id):
	db_user = Player.query.filter_by(username=player_id).first()
	if db_user is None:
		return make_response("No player found!", 400)

	UpdateLastOnline(player_id)

	#remove friend request
	player_requests = json.loads(db_user.friend_requests)
	player_requests.remove(invite_id)
	db_user.friend_requests = json.dumps(player_requests)

	db.session.commit()
	return jsonify({
		"success": True
	})

@app.route("/persist/friends_confirm_request_withmyinfoDW/", methods=['POST'])
def PersistFriendsConfirmRequestWithMyInfo():
	clientData = parse_qs(request.get_data().decode('utf-8'))
	clientData = {k: v[0] if len(v) == 1 else v for k, v in clientData.items()}

	UpdateLastOnline(clientData["player_id"])

	db_user = Player.query.filter_by(username=clientData["player_id"]).first()
	if db_user is None:
		return make_response("No player found!", 400)

	#Player ally check
	allycheck = AllyBoxSpaceNotExceeded(clientData["player_id"])
	if allycheck == False:
		return jsonify({
			"success": True,
			"info": "exceed me"
		})

	#friend ally check
	friendallycheck = AllyBoxSpaceNotExceeded(clientData["invite_id"])
	if friendallycheck == False:
		return jsonify({
			"success": True,
			"info": "exceed"
		})

	#remove friend request
	player_requests = json.loads(db_user.friend_requests)
	player_requests.remove(clientData["invite_id"])
	db_user.friend_requests = json.dumps(player_requests)

	#add friend
	player_friends = json.loads(db_user.friends)
	player_friends.append(clientData["invite_id"])
	db_user.friends = json.dumps(player_friends)

	#add self to friend's friend list
	friend_user = Player.query.filter_by(username=clientData["invite_id"]).first()

	if friend_user is None:
		return make_response("No player found!", 400)

	friend_friends = json.loads(friend_user.friends)
	friend_friends.append(clientData["player_id"])
	friend_user.friends = json.dumps(friend_friends)

	db.session.commit()

	return jsonify({
		"success": True
	})

@app.route("/persist/friends_remove/<string:player_id>/<string:invite_id>", methods=['GET'])
def PersistFriendsRemove(player_id, invite_id):
	db_user = Player.query.filter_by(username=player_id).first()
	if db_user is None:
		return make_response("No player found!", 400)

	#remove friend
	player_friends = json.loads(db_user.friends)
	player_friends.remove(invite_id)
	db_user.friends = json.dumps(player_friends)

	#remove self from friend
	friend_user = Player.query.filter_by(username=invite_id).first()

	if friend_user is None:
		return make_response("No player found!", 400)

	friend_friends = json.loads(friend_user.friends)
	friend_friends.remove(player_id)
	friend_user.friends = json.dumps(friend_friends)

	db.session.commit()

	return jsonify({
		"success": True
	})

@app.route("/analytics/upsight", methods=['POST'])
def AnalyticsUpsight():
	headers = request.headers

	if headers.get("Player-Id") is None or headers.get("Event-Type") is None or headers.get("Event-Action") is None:
		return make_response("Bad request!", 400)

	message = request.get_data().decode('utf-8')
	if message == "null":
		message = None

	newAnalytics = UpsightLogs(
		player_id=headers.get("Player-Id"),
		time=int(time.time()),
		event=headers.get("Event-Type"),
		action=headers.get("Event-Action"),
		message=message
	)
	db.session.add(newAnalytics)
	db.session.commit()

	if headers.get("Event-Action") == "detector":
		DiscordWebhookMessage(headers.get("Player-Id") + " triggered ACTk Anti-Cheat. Data:" + message)
		SystemBan(headers.get("Player-Id"))

	return make_response("OK", 200)

@app.route("/analytics/pvpmatch", methods=['POST'])
def AnalyticsPVPMatch():
	headers = request.headers

	if headers.get("Player-Id") is None:
		return make_response("Bad request!", 400)

	message = request.get_data().decode('utf-8')
	if message == "null":
		message = None

	#write to file
	os.makedirs("data/persist/pvpmatches", exist_ok=True)

	with open("data/persist/pvpmatches/" + headers.get("Player-Id", "unknown") +"_"+ headers.get("Match-Id", "unknown") + ".json", "w") as outfile:
		message = json.loads(message)
		json.dump(message, outfile, indent=4)

	return make_response("OK", 200)

@app.route("/dw_leaderboard/fetchentries/", methods=["POST"])
def FetchLeaderboardsEntriesEndpoint():
    try:
        payload = request.get_json(silent=True) or request.form

        start_pos = int(payload.get("startpos", 1))
        end_pos = int(payload.get("endpos", 30))

        pvp_ranks_file_path = "data/persist/blueprints/db_PVPRanks.json"

        leaderboard_list = []

        # Admin panel ile aynı veri kaynağı
        for player in Player.query.all():

            try:
                game = DecryptGameData(player.game)

                if game is None:
                    continue

                multiplayer_level = int(
                    game.get("MultiplayerLevel", 999)
                )

                points = int(
                    game.get("PointsInMultiplayerLevel", 0)
                )

                try:
                    rank_name, sprite_name, rank_id = get_rank_details_from_json(
                        points,
                        pvp_ranks_file_path
                    )
                except Exception:
                    rank_name = "Unknown"
                    sprite_name = ""
                    rank_id = 0

                leaderboard_list.append({
                    "playerid": player.username,
                    "playername": game.get(
                        "MultiplayerPlayerName",
                        "Unknown"
                    ),
                    "multiplayer_level": multiplayer_level,
                    "score": points,
                    "rank_name": rank_name,
                    "sprite_name": sprite_name,
                    "rank_id": rank_id
                })

            except Exception:
                continue

        # Admin panel ile aynı sıralama
        leaderboard_list.sort(
            key=lambda x: (
                x["multiplayer_level"] == -1,
                x["multiplayer_level"],
                -x["score"]
            )
        )

        # Ranking numarası ver
        for index, item in enumerate(leaderboard_list, start=1):
            item["ranking"] = index

        # En az 30 oyuncu
        minimum = max(30, end_pos)

        while len(leaderboard_list) < minimum:

            bot_index = len(leaderboard_list) + 1

            fake_points = max(
                100,
                22000 - bot_index * 650
            )

            try:
                rank_name, sprite_name, rank_id = get_rank_details_from_json(
                    fake_points,
                    pvp_ranks_file_path
                )
            except Exception:
                rank_name = "Unknown"
                sprite_name = ""
                rank_id = 0

            leaderboard_list.append({
                "ranking": bot_index,
                "playerid": f"bot_{bot_index}",
                "playername": f"Bot_{bot_index}",
                "multiplayer_level": 999,
                "score": fake_points,
                "rank_name": rank_name,
                "sprite_name": sprite_name,
                "rank_id": rank_id
            })

        return jsonify({
            "success": True,
            "data": leaderboard_list[start_pos - 1:end_pos]
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/dw_leaderboard/placeme/", methods=['POST'])
def place_me_on_leaderboard():
    try:
        req_data = request.get_json(force=True) if request.is_json else request.form
        # Oyuncuyu liderlik tablosuna kaydetme mantığı buraya gelir

        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/dw_leaderboard/registerresult/", methods=['POST'])
def register_match_result():
    try:
        req_data = request.get_json(force=True) if request.is_json else request.form
        # Maç sonucuna göre oyuncunun yeni puanını hesaplama mantığı buraya gelir

        # Örnek olarak oyuncunun yeni puanının 1500 olduğunu varsayalım
        # Unity callback((int)data["data"], ResponseFlag.Success) bekliyor
        return jsonify({"success": True, "data": 1500}), 200
    except Exception as e:
        return jsonify({"success": False, "data": 0}), 500


@app.route("/dw_leaderboard/hasended/", methods=['POST'])
def has_season_ended():
    try:
        # Sezon bitti mi kontrolü (0: Bitmedi, 1: Bitti)
        return jsonify({"success": True, "data": 0}), 200
    except Exception as e:
        return jsonify({"success": False, "data": 0}), 500

def GetPlayerWins(player_id):
	db_user = Player.query.filter_by(username=player_id).first()
	if db_user is None:
		return None

	if db_user.game is None or db_user.game == b"" or db_user.game == b" ":
		return None

	#check if player is game banned
	if IsUserBanned(player_id):
		return None

	#decrypt game
	try:
		game = DecryptGameData(db_user.game)
	except Exception:
		return None

	#check if user is PVP banned
	if game["Zxcvbnm"]:
		return None

	#which season is it?
	currentSeason = ""
	with open('data/persist/blueprints/db_PVPSeasons.json') as f:
		seasons = json.load(f)
		seasons = list(filter(lambda x: "EndDate" in x, seasons))
		#find the current season
		for season in seasons:
			#convert enddate to unix time
			if int(time.time()) < datetime.strptime(season["EndDate"], "%m/%d/%Y").timestamp():
				currentSeason = season["Season"]
				break
		#if the time is after enddate, use the last season
		if currentSeason == "":
			currentSeason = seasons[-1]["Season"]

	if game["ActivePvpSeason"] != currentSeason:
		return None

	if int(game["PvpPlayed"]) == 0:
		return None

	#count up wins
	#for each youWon: true in each battle history, add 1
	wins = 0
	for battle in game["BattleHistory"]:
		if battle["youWon"] == True and battle["season"] == currentSeason:
			wins += 1
	return wins

def run_scheduler():
	schedule.every(40000).hours.do(lambda: Backup())
	while True:
		try:
			schedule.run_pending()
			time.sleep(1)
		except Exception as e:
			Log("admin", f"Scheduler crashed: {str(e)}")


def Log(category, message):
	os.makedirs("data/persist/logs", exist_ok=True)
	date = datetime.now().strftime("%Y-%m-%d")
	time = datetime.now().strftime("%H:%M:%S")
	with open("data/persist/logs/" + date + ".txt", "a") as f:
		log = f"{time} - [{category.upper()}] - {message} \n"
		f.write(log)

def RunRemoveGameItemsStartupCleanup():
    """Clean configured Inventory IDs from active player databases on web-app reload."""
    if not REMOVE_GAME_ITEMS_ENABLED:
        app.logger.info("[RemoveGameItems] Disabled; startup cleanup skipped.")
        return

    target = str(REMOVE_GAME_ITEMS_DATABASE_TARGET or "localinstance").strip().lower()
    database_paths = []

    if target in ("primary", "all"):
        primary_path = os.path.join(app.instance_path, "cardwarskingdom.db")
        if os.path.isfile(primary_path):
            database_paths.append(("Primary", primary_path))

    if target in ("localinstance", "all"):
        local_path = _local_instance_db_path()
        if local_path and os.path.isfile(local_path):
            database_paths.append(("LocalInstance", local_path))

    if target not in ("primary", "localinstance", "all"):
        app.logger.error(
            "[RemoveGameItems] Invalid DATABASE_TARGET=%r; cleanup skipped.",
            target
        )
        return

    if not database_paths:
        app.logger.info(
            "[RemoveGameItems] No active database found for target %s.",
            target
        )
        return

    for database_label, database_path in database_paths:
        connection = None
        try:
            connection = sqlite3.connect(database_path, timeout=30)
            connection.row_factory = sqlite3.Row
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "player" not in tables:
                app.logger.warning(
                    "[RemoveGameItems] %s database has no player table; skipped.",
                    database_label
                )
                continue

            columns = {
                row[1] for row in connection.execute('PRAGMA table_info("player")')
            }
            if not {"username", "game"}.issubset(columns):
                app.logger.warning(
                    "[RemoveGameItems] %s player table lacks username/game; skipped.",
                    database_label
                )
                continue

            pending_updates = []
            failed_saves = 0
            for row in connection.execute(
                'SELECT "username", "game" FROM "player" WHERE "game" IS NOT NULL'
            ):
                username = row["username"]
                game_text = row["game"]
                try:
                    updated_game, removed_count = remove_game_items(
                        game_text,
                        _decode_game_save_for_name_change,
                        _encode_game_save_with_name
                    )
                    if removed_count > 0:
                        pending_updates.append((updated_game, username, removed_count))
                except Exception:
                    failed_saves += 1
                    app.logger.exception(
                        "[RemoveGameItems] Could not decode save for player %s in %s; left unchanged.",
                        username,
                        database_label
                    )

            if not pending_updates:
                app.logger.info(
                    "[RemoveGameItems] %s: no configured inventory items found%s.",
                    database_label,
                    " (some saves could not be read)" if failed_saves else ""
                )
                continue

            # Make a one-time SQLite backup before changing any save data.
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = database_path + ".removegameitems-" + timestamp + ".bak"
            backup_connection = sqlite3.connect(backup_path, timeout=30)
            try:
                connection.backup(backup_connection)
            finally:
                backup_connection.close()

            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    'UPDATE "player" SET "game" = ? WHERE "username" = ?',
                    [(updated_game, username) for updated_game, username, _ in pending_updates]
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

            total_removed = sum(count for _, _, count in pending_updates)
            app.logger.warning(
                "[RemoveGameItems] %s: removed %s inventory object(s) from %s player save(s). Backup: %s",
                database_label,
                total_removed,
                len(pending_updates),
                backup_path
            )
            if failed_saves:
                app.logger.warning(
                    "[RemoveGameItems] %s: %s player save(s) were skipped because decoding failed.",
                    database_label,
                    failed_saves
                )
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            app.logger.exception(
                "[RemoveGameItems] Startup cleanup failed for %s database.",
                database_label
            )
        finally:
            if connection is not None:
                connection.close()


# Create database tables, run migrations, then clean configured saves on every web-app reload.
with app.app_context():
        db.create_all()
        MigrateAdminSecurity()
        MigrateBanExpiration()
        MigratePlayerNameChangeCount()
        _ensure_google_account_tables()
        RunRemoveGameItemsStartupCleanup()


if __name__ == '__main__':
    Log("server", "Starting server...")

    # create version.txt and android_version.txt if they don't exist
    if not os.path.exists("data/persist/version.txt"):
        with open("data/persist/version.txt", "w") as f:
            f.write("1.0.0")

    if not os.path.exists("data/persist/android_version.txt"):
        with open("data/persist/android_version.txt", "w") as f:
            f.write("1.0.0")

    # Start scheduler
    scheduler_thread = threading.Thread(
        target=run_scheduler,
        daemon=True
    )
    scheduler_thread.start()

    # Start Flask server LAST
    app.run(
        debug=args.debug,
        port=args.port
    )


# ========================= BALANCER PANEL =========================
# The official Clash Royale API is used only as an external reference.
# CWK-specific balance values stay in local blueprint JSON files.
import urllib.error
import urllib.request

BALANCER_BLUEPRINT_DIR = os.path.join("data", "persist", "blueprints")
CLASH_ROYALE_API_BASE = os.environ.get(
    "CLASH_ROYALE_API_BASE", "https://api.clashroyale.com/v1"
).rstrip("/")
CLASH_ROYALE_API_TOKEN = os.environ.get("CLASH_ROYALE_API_TOKEN", "").strip()

# ======================= BALANCER LANGUAGE DATA =======================
# Language files and approval state are tracked by data/persist/languagedata/manifest.json.
import xml.etree.ElementTree as ET

BALANCER_LANGUAGE_DATA_DIR = os.path.join("data", "persist", "languagedata")
BALANCER_LANGUAGE_MANIFEST = os.path.join(BALANCER_LANGUAGE_DATA_DIR, "manifest.json")
BALANCER_LANGUAGES = (
    ("AR", "العربية", "AR_Sheet1.txt"),
    ("EN", "English", "EN_Sheet1.txt"),
    ("ES", "Español", "ES_Sheet1.txt"),
    ("FR", "Français", "FR_Sheet1.txt"),
    ("IT", "Italiano", "IT_Sheet1.txt"),
    ("PT", "Português", "PT_Sheet1.txt"),
    ("TR", "Türkçe", "TR_Sheet1.txt"),
    ("VI", "Tiếng Việt", "VI_Sheet1.txt"),
)


def _balancer_language_default_manifest():
    return {
        "version": 1,
        "languages": [
            {"code": code, "name": name, "file": filename, "status": "pending"}
            for code, name, filename in BALANCER_LANGUAGES
        ]
    }


def _balancer_language_manifest():
    os.makedirs(BALANCER_LANGUAGE_DATA_DIR, exist_ok=True)
    if not os.path.isfile(BALANCER_LANGUAGE_MANIFEST):
        manifest = _balancer_language_default_manifest()
        _balancer_write_language_manifest(manifest, create_only=True)

    with open(BALANCER_LANGUAGE_MANIFEST, "r", encoding="utf-8-sig") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("languages"), list):
        raise ValueError("manifest.json must contain a languages array.")

    existing = {
        str(item.get("code", "")).upper(): item
        for item in manifest["languages"] if isinstance(item, dict)
    }
    languages = []
    for code, name, filename in BALANCER_LANGUAGES:
        item = existing.get(code, {})
        status = item.get("status", "pending")
        if status not in ("pending", "approved"):
            status = "pending"
        actual_filename = filename
        if code == "EN" and not os.path.isfile(os.path.join(BALANCER_LANGUAGE_DATA_DIR, actual_filename)):
            if os.path.isfile(os.path.join(BALANCER_LANGUAGE_DATA_DIR, "EN_Sheet1.txt")):
                actual_filename = "EN_Sheet1.txt"
        languages.append({
            "code": code,
            "name": name,
            "file": actual_filename,
            "status": status,
            "detected": os.path.isfile(os.path.join(BALANCER_LANGUAGE_DATA_DIR, actual_filename))
        })
    manifest["version"] = 1
    manifest["languages"] = languages
    return manifest


def _balancer_write_language_manifest(manifest, create_only=False):
    os.makedirs(BALANCER_LANGUAGE_DATA_DIR, exist_ok=True)
    path = BALANCER_LANGUAGE_MANIFEST
    if not create_only and os.path.isfile(path) and not os.path.exists(path + ".balancer.bak"):
        shutil.copy2(path, path + ".balancer.bak")
    temp_path = path + ".balancer.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        with open(temp_path, "r", encoding="utf-8") as handle:
            checked = json.load(handle)
        if not isinstance(checked, dict) or not isinstance(checked.get("languages"), list):
            raise ValueError("Generated language manifest failed validation.")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


_BALANCER_EN_LOCALIZATION_CACHE = {"path": None, "mtime": None, "values": {}}


def _balancer_localized_text(value):
    """Resolve !! localization keys from the game's <entries><entry name="...">text</entry></entries> format."""
    text_value = str(value or "").strip()
    if not text_value.startswith("!!"):
        return text_value

    # PythonAnywhere may run the app from a different working directory.
    # Check both the configured data folder and the requested persist path.
    language_dirs = (
        BALANCER_LANGUAGE_DATA_DIR,
        os.path.join(app.root_path, "data", "persist", "languagedata"),
        os.path.join(app.root_path, "persist", "languagedata"),
        os.path.join(os.getcwd(), "data", "persist", "languagedata"),
        os.path.join(os.getcwd(), "persist", "languagedata"),
        os.path.join(os.sep, "persist", "languagedata"),
    )
    candidates = []
    for directory in dict.fromkeys(language_dirs):
        candidates.extend((
            os.path.join(directory, "EN_Sheet1.txt"),
            os.path.join(directory, "EN_Sheet1.xml"),
        ))
    path = next((item for item in candidates if os.path.isfile(item)), None)
    if not path:
        return text_value

    try:
        mtime = os.path.getmtime(path)
        cache = _BALANCER_EN_LOCALIZATION_CACHE
        if cache["path"] != path or cache["mtime"] != mtime:
            with open(path, "r", encoding="utf-8-sig") as handle:
                content = handle.read()

            values = {}
            # EN_Sheet1.txt may itself contain XML; the .txt extension is intentional.
            if content.lstrip().startswith("<"):
                try:
                    root = ET.fromstring(content)
                    for entry in root.iter():
                        if entry.tag.split("}")[-1].casefold() != "entry":
                            continue
                        key = (entry.get("name") or "").strip()
                        if key:
                            values[key] = "".join(entry.itertext()).strip()
                except ET.ParseError:
                    app.logger.warning("Balancer localization XML is invalid: %s", path)

            # Retain compatibility with legacy KEY = VALUE text files.
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("//") or "=" not in line:
                    continue
                key, translated = line.split("=", 1)
                key = key.strip()
                if key.startswith("!!"):
                    values[key] = translated.strip().strip('"')

            cache.update({"path": path, "mtime": mtime, "values": values})

        values = cache["values"]
        if text_value in values:
            return values[text_value]
        folded = text_value.casefold()
        for key, translated in values.items():
            if key.casefold() == folded:
                return translated
        return text_value
    except OSError:
        app.logger.warning("Could not read Balancer localization file: %s", path, exc_info=True)
        return text_value


def _balancer_language_files():
    manifest = _balancer_language_manifest()
    return [
        item["file"] for item in manifest["languages"]
        if item["detected"]
        and os.path.isfile(os.path.join(BALANCER_LANGUAGE_DATA_DIR, item["file"]))
    ]


def _balancer_language_path(filename):
    allowed = {item[2] for item in BALANCER_LANGUAGES}
    if not filename or filename not in allowed or filename not in _balancer_language_files():
        raise FileNotFoundError("Language file not found or not listed in manifest.")
    return os.path.join(BALANCER_LANGUAGE_DATA_DIR, filename)


def _balancer_read_language_file(filename):
    path = _balancer_language_path(filename)
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    try:
        tree = ET.parse(path, parser=parser)
    except ET.ParseError as exc:
        raise ValueError("Invalid language XML: " + str(exc))
    if tree.getroot().tag != "entries":
        raise ValueError("Root element must be <entries>.")
    entries = []
    for element in tree.getroot().findall(".//entry"):
        key = (element.get("name") or "").strip()
        if key:
            entries.append({"key": key, "value": element.text or ""})
    return tree, entries


def _balancer_write_language_file(filename, tree):
    path = _balancer_language_path(filename)
    backup_path = path + ".balancer.bak"
    if not os.path.exists(backup_path):
        shutil.copy2(path, backup_path)
    temp_path = path + ".balancer.tmp"
    try:
        ET.indent(tree, space="  ")
        tree.write(temp_path, encoding="utf-8", xml_declaration=True)
        ET.parse(temp_path)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


@app.route("/balancer/language-data", methods=["GET", "POST"])
def BalancerLanguageData():
    account = get_balancer_account()
    if not account:
        return redirect("/balancer/login?next=/balancer/language-data")

    error = None
    success = None
    try:
        manifest = _balancer_language_manifest()
        files = [item["file"] for item in manifest["languages"] if item["detected"]]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return make_response("Language manifest error: " + str(exc), 500)

    filename = request.values.get("file", "").strip()
    if filename not in files:
        filename = files[0] if files else ""
    query = request.values.get("q", "").strip()[:200]
    selected_key = request.values.get("key", "").strip()[:500]
    edited_value = request.form.get("value", "")
    entries = []

    if request.method == "POST":
        action = request.form.get("action", "")
        code = request.form.get("language_code", "").strip().upper()
        if action in ("approve_language", "revoke_language"):
            language = next((item for item in manifest["languages"] if item["code"] == code), None)
            if not language:
                error = "Unsupported language code."
            elif not language["detected"]:
                error = "Upload " + language["file"] + " before approving this language."
            elif action == "approve_language":
                language["status"] = "approved"
                success = code + " language approved."
            else:
                language["status"] = "pending"
                success = code + " approval revoked; it is pending review again."
            if not error:
                _balancer_write_language_manifest(manifest)
                manifest = _balancer_language_manifest()
                Log("balancer", account.username + " changed language approval: " + code + " -> " + language["status"])
                files = [item["file"] for item in manifest["languages"] if item["detected"]]
                if filename not in files:
                    filename = files[0] if files else ""
        elif action == "save_entry":
            if not filename or filename not in files:
                error = "Select an existing language file before saving."
            elif not selected_key:
                error = "Select a language key before saving."
            else:
                try:
                    tree, entries = _balancer_read_language_file(filename)
                    targets = [
                        item for item in tree.getroot().findall(".//entry")
                        if (item.get("name") or "").strip() == selected_key
                    ]
                    if len(targets) != 1:
                        raise ValueError("Key was not found or is duplicated; file was not changed.")
                    targets[0].text = edited_value
                    _balancer_write_language_file(filename, tree)
                    # Any translation edit invalidates the previous approval and requires review again.
                    language = next((item for item in manifest["languages"] if item["file"] == filename), None)
                    if language and language["status"] == "approved":
                        language["status"] = "pending"
                        _balancer_write_language_manifest(manifest)
                        manifest = _balancer_language_manifest()
                    Log("balancer", account.username + " edited language key " + selected_key + " in " + filename)
                    success = "Translation saved. The language is pending review if it was previously approved. The first-edit .balancer.bak backup is preserved."
                except (OSError, ValueError, ET.ParseError) as exc:
                    error = str(exc)
        else:
            error = "Invalid action."

    selected_language = next(
        (item for item in manifest["languages"] if item["file"] == filename), None
    )
    if filename and not error:
        try:
            tree, entries = _balancer_read_language_file(filename)
        except (OSError, ValueError, ET.ParseError) as exc:
            error = str(exc)

    selected_entry = next((row for row in entries if row["key"] == selected_key), None)
    if not selected_key and entries:
        selected_key = entries[0]["key"]
        selected_entry = entries[0]
    if request.method == "GET" or success:
        edited_value = selected_entry["value"] if selected_entry else ""

    filtered_entries = entries
    if query:
        q_lower = query.casefold()
        filtered_entries = [
            row for row in entries
            if q_lower in row["key"].casefold() or q_lower in row["value"].casefold()
        ]

    return render_template(
        "balancer_language_data.html", admin_name=account.username,
        manifest=manifest, files=files, filename=filename,
        selected_language=selected_language, entries=filtered_entries[:2000],
        query=query, selected_key=selected_key, edited_value=edited_value,
        error=error, success=success
    )
# ===================== END BALANCER LANGUAGE DATA =======================

# Keep third-party card data on the server, never in Flask's client-side cookie.
_BALANCER_CR_CARDS_CACHE = {"time": 0, "cards": None}


@app.before_request
def _balancer_clear_legacy_cookie_cache():
    # Older versions stored the full Clash Royale card list in the session cookie,
    # making the cookie exceed browsers' ~4 KB limit. Remove it for existing sessions.
    if "clash_royale_cards_cache" in session:
        session.pop("clash_royale_cards_cache", None)


def _balancer_read_blueprint(filename):
    path = os.path.join(BALANCER_BLUEPRINT_DIR, filename)
    if not os.path.isfile(path):
        raise FileNotFoundError("Blueprint file not found: " + filename)
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(filename + " must contain a JSON array")
    return payload


def _balancer_cr_cards(force_refresh=False):
    """Fetch Clash Royale cards and cache them in server memory, not the session cookie."""
    if not CLASH_ROYALE_API_TOKEN:
        return None, "CLASH_ROYALE_API_TOKEN is not configured."

    now = int(time.time())
    cached_cards = _BALANCER_CR_CARDS_CACHE.get("cards")
    cached_time = int(_BALANCER_CR_CARDS_CACHE.get("time", 0) or 0)
    if not force_refresh and isinstance(cached_cards, list) and now - cached_time < 900:
        return cached_cards, None

    req = urllib.request.Request(
        CLASH_ROYALE_API_BASE + "/cards",
        headers={
            "Authorization": "Bearer " + CLASH_ROYALE_API_TOKEN,
            "Accept": "application/json",
            "User-Agent": "CWK-Balancer/1.0"
        },
        method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
        cards = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(cards, list):
            return None, "Unexpected response from Clash Royale API."
        _BALANCER_CR_CARDS_CACHE["time"] = now
        _BALANCER_CR_CARDS_CACHE["cards"] = cards
        return cards, None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            message = "Clash Royale API rejected the token (401)."
        elif exc.code == 403:
            message = "Clash Royale API denied access (403). Check the token IP allowlist."
        elif exc.code == 429:
            message = "Clash Royale API rate limit reached (429)."
        else:
            message = "Clash Royale API returned HTTP " + str(exc.code) + "."
        return None, message
    except Exception as exc:
        app.logger.warning("Balancer Clash Royale request failed: %s", repr(exc))
        return None, "Could not connect to Clash Royale API."


def _balancer_normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _balancer_build_report(creatures, action_cards, clash_cards):
    report = {
        "creatures": {"total": len(creatures), "duplicate_ids": [], "missing_fields": [],
                      "invalid_numbers": [], "broken_action_refs": [], "rows": []},
        "action_cards": {"total": len(action_cards), "duplicate_ids": [], "missing_fields": [],
                         "invalid_numbers": [], "rows": []},
        "cross_game": {"matched": 0, "unmatched": 0, "matches": []},
    }
    creature_ids = set()
    action_ids = set()
    action_ref_ids = set()

    for row in action_cards:
        card_id = str(row.get("ID", "")).strip()
        if card_id:
            if card_id in action_ids:
                report["action_cards"]["duplicate_ids"].append(card_id)
            action_ids.add(card_id)
            action_ref_ids.add(card_id)
        missing = [key for key in ("ID", "Cost", "Rarity") if not str(row.get(key, "")).strip()]
        if missing:
            report["action_cards"]["missing_fields"].append({"id": card_id or "(no ID)", "fields": missing})
        invalid = []
        for key in ("Cost", "Rarity", "DirectDamage", "BurstSTR", "EnhanceSTR", "Heal", "Shield"):
            value = row.get(key)
            if value not in (None, ""):
                try:
                    float(value)
                except (TypeError, ValueError):
                    invalid.append(key)
        if invalid:
            report["action_cards"]["invalid_numbers"].append({"id": card_id or "(no ID)", "fields": invalid})
        report["action_cards"]["rows"].append({
            "id": card_id, "display_id": re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", card_id),
            "name_key": str(row.get("Name", "")).strip(),
            "name": _balancer_localized_text(row.get("Name", "")),
            "display_name": _balancer_localized_text(row.get("Name", "")),
            "cost": row.get("Cost", ""), "rarity": row.get("Rarity", ""),
            "type": row.get("TypeText", ""), "faction": row.get("Faction", ""),
            "damage": row.get("DirectDamage", row.get("BurstSTR", "")),
            "issues": len(missing) + len(invalid)
        })

    for row in creatures:
        creature_id = str(row.get("ID", "")).strip()
        if creature_id:
            if creature_id in creature_ids:
                report["creatures"]["duplicate_ids"].append(creature_id)
            creature_ids.add(creature_id)
        missing = [key for key in ("ID", "DeployCost", "Rarity", "BalHP", "BalSTR", "BalINT") if not str(row.get(key, "")).strip()]
        if missing:
            report["creatures"]["missing_fields"].append({"id": creature_id or "(no ID)", "fields": missing})
        invalid = []
        for key in ("DeployCost", "Rarity", "BalHP", "BalSTR", "BalINT", "BalDEX", "BalDEF", "BalRES", "MinHP", "MaxHP"):
            value = row.get(key)
            if value not in (None, ""):
                try:
                    float(value)
                except (TypeError, ValueError):
                    invalid.append(key)
        if invalid:
            report["creatures"]["invalid_numbers"].append({"id": creature_id or "(no ID)", "fields": invalid})
        refs = [str(row.get("ActionCard" + str(i), "")).strip() for i in range(1, 7)]
        refs = [value for value in refs if value]
        missing_refs = [value for value in refs if value not in action_ref_ids]
        if missing_refs:
            report["creatures"]["broken_action_refs"].append({"id": creature_id or "(no ID)", "references": missing_refs})
        report["creatures"]["rows"].append({
            "id": creature_id, "display_id": re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", creature_id),
            "name_key": str(row.get("Name", "")).strip(),
            "name": _balancer_localized_text(row.get("Name", "")),
            "display_name": _balancer_localized_text(row.get("Name", "")),
            "deploy_cost": row.get("DeployCost", ""), "rarity": row.get("Rarity", ""),
            "hp": row.get("BalHP", ""), "str": row.get("BalSTR", ""),
            "int": row.get("BalINT", ""), "dex": row.get("BalDEX", ""),
            "issues": len(missing) + len(invalid) + len(missing_refs)
        })

    # Clash Royale has a different data model. This is a name-reference check,
    # not a gameplay-equivalent balance comparison.
    normalized_cr = {}
    for card in clash_cards or []:
        key = _balancer_normalize(card.get("name"))
        if key and key not in normalized_cr:
            normalized_cr[key] = card

    for row in report["creatures"]["rows"] + report["action_cards"]["rows"]:
        key = _balancer_normalize(row.get("name", ""))
        match = normalized_cr.get(key) if key else None
        if not match:
            composite = _balancer_normalize(row.get("id", "") + " " + row.get("name", ""))
            candidates = [card for cr_key, card in normalized_cr.items()
                          if cr_key and (cr_key in composite or composite in cr_key)]
            if len(candidates) == 1:
                match = candidates[0]
        if match:
            report["cross_game"]["matched"] += 1
            report["cross_game"]["matches"].append({
                "local_id": row.get("id", ""),
                "local_name": row.get("name", ""),
                "clash_royale_name": match.get("name", ""),
                "elixir": match.get("elixirCost", ""),
                "rarity": match.get("rarity", ""),
                "type": match.get("type", "")
            })
        else:
            report["cross_game"]["unmatched"] += 1
    return report


@app.route("/balancer/login", methods=["GET", "POST"])
def BalancerLogin():
    if get_balancer_account():
        return redirect("/balancer/")
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        account = BalancerAccount.query.filter_by(username=username).first()
        if account and bcrypt.check_password_hash(account.password, password):
            session["balancer_username"] = account.username
            session.permanent = True
            Log("balancer", "Balancer account logged in: " + account.username)
            return redirect(request.args.get("next") if request.args.get("next", "").startswith("/balancer/") else "/balancer/")
        error = "Kullanıcı adı veya şifre hatalı."
    return render_template("balancer_login.html", error=error)


@app.route("/balancer/logout", methods=["POST", "GET"])
def BalancerLogout():
    username = session.pop("balancer_username", None)
    if username:
        Log("balancer", "Balancer account logged out: " + username)
    return redirect("/balancer/login")


@app.route("/admin/createbalancer", methods=["GET", "POST"])
@login_required
def AdminCreateBalancer():
    if not isAdmin(current_user):
        return abort(404)
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", username):
            error = "Kullanıcı adı 3-80 karakter olmalı; harf, rakam, nokta, tire ve alt çizgi kullanılabilir."
        elif len(password) < 10:
            error = "Şifre en az 10 karakter olmalı."
        elif password != confirm_password:
            error = "Şifreler eşleşmiyor."
        elif BalancerAccount.query.filter_by(username=username).first():
            error = "Bu Balancer kullanıcı adı zaten kullanılıyor."
        else:
            account = BalancerAccount(
                username=username,
                password=bcrypt.generate_password_hash(password).decode("utf-8"),
                created_by=current_user.username
            )
            db.session.add(account)
            db.session.commit()
            Log("admin", current_user.username + " created Balancer account: " + username)
            return redirect("/admin/createbalancer?created=" + username)
    return render_template(
        "create_balancers.html",
        error=error,
        created=request.args.get("created", "")
    )



def _balancer_write_blueprint(filename, payload):
    """Validate and atomically replace a blueprint JSON array; no database/schema changes."""
    if filename not in ("db_Creatures.json", "db_ActionCards.json"):
        raise ValueError("Unsupported blueprint file.")
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise ValueError("Blueprint must be a JSON array of objects.")

    path = os.path.join(BALANCER_BLUEPRINT_DIR, filename)
    if not os.path.isfile(path):
        raise FileNotFoundError("Blueprint file not found: " + filename)

    backup_path = path + ".balancer.bak"
    if not os.path.exists(backup_path):
        shutil.copy2(path, backup_path)

    temp_path = path + ".balancer.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        with open(temp_path, "r", encoding="utf-8") as handle:
            checked = json.load(handle)
        if not isinstance(checked, list) or any(not isinstance(row, dict) for row in checked):
            raise ValueError("Saved JSON failed validation.")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


@app.route("/balancer/editor", methods=["GET", "POST"])
def BalancerEditor():
    balancer_account = get_balancer_account()
    if not balancer_account:
        return redirect("/balancer/login?next=/balancer/editor")

    filenames = {"creatures": "db_Creatures.json", "action_cards": "db_ActionCards.json"}
    dataset = request.values.get("dataset", "creatures")
    if dataset not in filenames:
        dataset = "creatures"

    error = None
    success = None
    records = []
    selected_id = request.values.get("record_id", "").strip()
    editor_json = request.form.get("record_json", "")

    try:
        records = _balancer_read_blueprint(filenames[dataset])
        if request.method == "POST":
            if request.form.get("action", "save_record") != "save_record":
                raise ValueError("Geçersiz işlem.")
            if not selected_id:
                raise ValueError("Önce düzenlenecek kayıt ID'sini seçin.")
            try:
                edited = json.loads(editor_json)
            except json.JSONDecodeError as exc:
                raise ValueError("JSON sözdizimi hatalı: " + str(exc))
            if not isinstance(edited, dict):
                raise ValueError("Kayıt bir JSON nesnesi olmalıdır.")
            if str(edited.get("ID", "")).strip() != selected_id:
                raise ValueError("ID alanı seçili kaydın ID'siyle aynı kalmalıdır.")

            matches = [i for i, row in enumerate(records)
                       if str(row.get("ID", "")).strip() == selected_id]
            if len(matches) != 1:
                raise ValueError("ID bulunamadı veya benzersiz değil; dosya değiştirilmedi.")

            records[matches[0]] = edited
            _balancer_write_blueprint(filenames[dataset], records)
            Log("balancer", balancer_account.username + " edited " + filenames[dataset] + " record " + selected_id)
            success = "Kayıt kaydedildi. İlk düzenleme öncesindeki .balancer.bak yedeği korunuyor."

        if request.method == "GET" or success:
            chosen = next((row for row in records if str(row.get("ID", "")).strip() == selected_id), None)
            if chosen is None and records:
                chosen = records[0]
                selected_id = str(chosen.get("ID", "")).strip()
            editor_json = json.dumps(chosen or {}, ensure_ascii=False, indent=4)

    except (OSError, ValueError, json.JSONDecodeError) as exc:
        error = str(exc)
        if not editor_json:
            editor_json = "{}"

    record_options = []
    for row in records:
        raw_id = str(row.get("ID", "")).strip()
        name_key = str(row.get("Name", "")).strip()
        localized_name = _balancer_localized_text(name_key)
        # Make CamelCase IDs easier to scan in the dropdown, without changing the real ID.
        display_id = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw_id)
        record_options.append({
            "id": raw_id,
            "display_id": display_id,
            "name_key": name_key,
            # Do not show an unresolved !!KEY as if it were a display name.
            "name": "" if localized_name == name_key and name_key.startswith("!!") else localized_name,
        })
    selected_record = next((row for row in records if str(row.get("ID", "")).strip() == selected_id), {})
    selected_name_key = str(selected_record.get("Name", "")).strip()
    selected_localized_name = _balancer_localized_text(selected_name_key)
    if selected_name_key.startswith("!!") and selected_localized_name == selected_name_key:
        selected_localized_name = ""
    return render_template(
        "balancer_editor.html",
        admin_name=balancer_account.username,
        dataset=dataset,
        record_options=record_options,
        selected_id=selected_id,
        selected_name_key=selected_name_key,
        selected_localized_name=selected_localized_name,
        record_json=editor_json,
        error=error,
        success=success
    )


@app.route("/balancer/", methods=["GET", "POST"])
def BalancerPanel():
    balancer_account = get_balancer_account()
    if not balancer_account:
        return redirect("/balancer/login?next=/balancer/")

    force_refresh = request.method == "POST" and request.form.get("action") == "refresh_api"
    try:
        creatures = _balancer_read_blueprint("db_Creatures.json")
        action_cards = _balancer_read_blueprint("db_ActionCards.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        app.logger.exception("Balancer blueprint load failed")
        return make_response("Balancer data error: " + str(exc), 500)

    clash_cards, api_error = _balancer_cr_cards(force_refresh=force_refresh)
    report = _balancer_build_report(creatures, action_cards, clash_cards)
    if force_refresh:
        Log("balancer", balancer_account.username + " refreshed the Balancer Clash Royale data.")
        return redirect("/balancer/")

    query = request.args.get("q", "").strip()[:100].casefold()
    selected_tab = request.args.get("tab", "creatures")
    if selected_tab not in ("creatures", "action_cards", "matches"):
        selected_tab = "creatures"
    for key in ("creatures", "action_cards"):
        rows = report[key]["rows"]
        if query:
            rows = [row for row in rows if query in str(row.get("id", "")).casefold()
                    or query in str(row.get("name", "")).casefold()]
        report[key]["filtered_rows"] = rows[:1000]
    matches = report["cross_game"]["matches"]
    if query:
        matches = [row for row in matches if query in str(row.get("local_id", "")).casefold()
                   or query in str(row.get("local_name", "")).casefold()
                   or query in str(row.get("clash_royale_name", "")).casefold()]
    report["cross_game"]["filtered_matches"] = matches[:1000]

    return render_template(
        "balancer.html",
        report=report,
        api_error=api_error,
        api_configured=bool(CLASH_ROYALE_API_TOKEN),
        api_count=len(clash_cards or []),
        query=query,
        selected_tab=selected_tab,
        admin_name=balancer_account.username
    )
# ======================= END BALANCER PANEL =======================