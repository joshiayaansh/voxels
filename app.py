import sqlite3
import re
import json
import time
from pathlib import Path
import os
from threading import Lock
import dotenv
dotenv.load_dotenv()
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

socketio = SocketIO(app, cors_allowed_origins="*")

active_rooms = {}
sid_presence = {}
presence_lock = Lock()

DB_PATH = Path(__file__).with_name("accounts.db")
DEFAULT_GAME_CONFIG_PATH = Path(__file__).parent / "static" / "game" / "default.json"
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_hex_color(value, fallback):
    if value and HEX_COLOR_RE.fullmatch(value):
        return value.lower()
    return fallback


def load_default_game_config():
    if not DEFAULT_GAME_CONFIG_PATH.exists():
        return {}
    with DEFAULT_GAME_CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def parse_game_config(config_text):
    parsed = json.loads(config_text)
    if not isinstance(parsed, dict):
        raise ValueError("Game config must be a JSON object.")
    level = parsed.get("level")
    if not isinstance(level, dict):
        raise ValueError("Game config must contain a 'level' object.")
    parts = level.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("Game config level.parts must be a non-empty array.")
    return parsed


def init_db():
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            avatar_color TEXT DEFAULT '#ffdac1',
            avatar_face TEXT DEFAULT 'smile',
            avatar_clothes TEXT DEFAULT 'tshirt',
            avatar_pants TEXT DEFAULT 'basic',
            avatar_arms TEXT DEFAULT 'basic',
            avatar_wristwear TEXT DEFAULT 'none',
            avatar_head_color TEXT DEFAULT '#ffdac1',
            avatar_torso_color TEXT DEFAULT '#3b82f6',
            avatar_arms_color TEXT DEFAULT '#ffdac1',
            avatar_legs_color TEXT DEFAULT '#1e293b',
            pixels INTEGER DEFAULT 150,
            owned_faces TEXT DEFAULT 'smile',
            owned_clothes TEXT DEFAULT 'tshirt',
            owned_pants TEXT DEFAULT 'basic',
            owned_arms TEXT DEFAULT 'basic',
            owned_wristwear TEXT DEFAULT 'none'
        )
        """
    )
    try:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_color TEXT DEFAULT '#ffdac1'")
        conn.execute("ALTER TABLE users ADD COLUMN avatar_face TEXT DEFAULT 'smile'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_clothes TEXT DEFAULT 'tshirt'")
        conn.execute("ALTER TABLE users ADD COLUMN pixels INTEGER DEFAULT 150")
        conn.execute("ALTER TABLE users ADD COLUMN owned_faces TEXT DEFAULT 'smile'")
        conn.execute("ALTER TABLE users ADD COLUMN owned_clothes TEXT DEFAULT 'tshirt'")
    except sqlite3.OperationalError:
        pass
    for statement in [
        "ALTER TABLE users ADD COLUMN avatar_pants TEXT DEFAULT 'basic'",
        "ALTER TABLE users ADD COLUMN avatar_head_color TEXT DEFAULT '#ffdac1'",
        "ALTER TABLE users ADD COLUMN avatar_torso_color TEXT DEFAULT '#3b82f6'",
        "ALTER TABLE users ADD COLUMN avatar_arms_color TEXT DEFAULT '#ffdac1'",
        "ALTER TABLE users ADD COLUMN avatar_legs_color TEXT DEFAULT '#1e293b'",
        "ALTER TABLE users ADD COLUMN owned_pants TEXT DEFAULT 'basic'",
        "ALTER TABLE users ADD COLUMN avatar_arms TEXT DEFAULT 'basic'",
        "ALTER TABLE users ADD COLUMN avatar_wristwear TEXT DEFAULT 'none'",
        "ALTER TABLE users ADD COLUMN owned_arms TEXT DEFAULT 'basic'",
        "ALTER TABLE users ADD COLUMN owned_wristwear TEXT DEFAULT 'none'",
    ]:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            thumbnail_url TEXT DEFAULT '',
            config_json TEXT NOT NULL,
            script_js TEXT DEFAULT '',
            is_public INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (creator_id) REFERENCES users (id)
        )
        """
    )
    try:
        conn.execute("ALTER TABLE games ADD COLUMN thumbnail_url TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS appeals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'open',
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        """
    )
    for statement in [
        "ALTER TABLE users ADD COLUMN is_disabled INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


FACES = {
    "smile": {"name": "Smile", "price": 0},
    "angry": {"name": "Angry", "price": 50},
    "super_super_angry": {"name": "Super Super Angry", "price": 80000},
    "surprised": {"name": "Surprised", "price": 50},
    "cool": {"name": "Cool (Sunglasses)", "price": 100},
    "demon": {"name": "Demon", "price": 150},
}

CLOTHES = {
    "tshirt": {"name": "T-Shirt", "price": 0},
    "hoodie": {"name": "Hoodie", "price": 50},
    "suit": {"name": "Suit", "price": 150},
    "ninja": {"name": "Ninja Suit", "price": 200},
}

PANTS = {
    "basic": {"name": "Basic Pants", "price": 0},
    "jeans": {"name": "Jeans", "price": 50},
    "cargo": {"name": "Cargo Pants", "price": 90},
    "formal": {"name": "Formal Pants", "price": 120},
}

ARMS = {
    "basic": {"name": "Basic Arms", "price": 0},
    "armored": {"name": "Armored Arms", "price": 80},
    "robotic": {"name": "Robotic Arms", "price": 140},
}

WRISTWEAR = {
    "none": {"name": "No Wristwear", "price": 0},
    "band": {"name": "Sport Band", "price": 60},
    "watch": {"name": "Digital Watch", "price": 110},
}

def is_logged_in():
    return bool(session.get("user_id") and session.get("username"))


def get_current_user():
    if not is_logged_in():
        return None
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    return user


@app.route("/", methods=["GET", "POST"])
def login_page():
    if is_logged_in():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Please enter username and password.")
            return render_template("login.html")

        conn = get_db_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username or password.")
            return render_template("login.html")

        if user["is_disabled"]:
            flash("This account has been disabled. Submit an appeal below if you think this is a mistake.")
            return render_template("login.html")

        session.permanent = True
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["display_name"] = user["display_name"]
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup_page():
    if is_logged_in():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not all([display_name, username, email, password]):
            flash("All fields are required.")
            return render_template("signup.html")

        conn = get_db_connection()
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ? OR email = ?", (username, email)
        ).fetchone()
        if existing:
            conn.close()
            flash("Username or email already exists.")
            return render_template("signup.html")

        conn.execute(
            "INSERT INTO users (display_name, username, email, password_hash) VALUES (?, ?, ?, ?)",
            (display_name, username, email, generate_password_hash(password)),
        )
        conn.commit()
        user = conn.execute(
            "SELECT id, username, display_name FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()

        session.permanent = True
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["display_name"] = user["display_name"]
        return redirect(url_for("dashboard"))

    return render_template("signup.html")


@app.route("/dashboard")
def dashboard():
    if not is_logged_in():
        return redirect(url_for("login_page"))
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    feed_games = conn.execute(
        """
        SELECT games.id, games.title, games.description, games.thumbnail_url, games.created_at, users.username AS creator_username
        FROM games
        JOIN users ON users.id = games.creator_id
        WHERE games.is_public = 1
        ORDER BY games.id DESC
        """
    ).fetchall()
    my_games_list = conn.execute(
        """
        SELECT games.id, games.title, games.description, games.thumbnail_url, games.created_at
        FROM games
        WHERE games.creator_id = ?
        ORDER BY games.id DESC
        """,
        (session["user_id"],),
    ).fetchall()
    conn.close()
    
    owned_faces = user["owned_faces"].split(",") if user["owned_faces"] else []
    owned_clothes = user["owned_clothes"].split(",") if user["owned_clothes"] else []
    owned_pants = user["owned_pants"].split(",") if user["owned_pants"] else []
    owned_arms = user["owned_arms"].split(",") if user["owned_arms"] else []
    owned_wristwear = user["owned_wristwear"].split(",") if user["owned_wristwear"] else []
    
    return render_template(
        "dashboard.html", 
        user=user, 
        FACES=FACES, 
        CLOTHES=CLOTHES,
        PANTS=PANTS,
        ARMS=ARMS,
        WRISTWEAR=WRISTWEAR,
        owned_faces=owned_faces,
        owned_clothes=owned_clothes,
        owned_pants=owned_pants,
        owned_arms=owned_arms,
        owned_wristwear=owned_wristwear,
        feed_games=feed_games,
        my_games_list=my_games_list,
    )


@app.route("/update_account", methods=["POST"])
def update_account():
    if not is_logged_in():
        return redirect(url_for("login_page"))
    display_name = request.form.get("display_name", "").strip()
    if display_name:
        conn = get_db_connection()
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, session["user_id"]))
        conn.commit()
        conn.close()
        session["display_name"] = display_name
        flash("Account updated successfully!")
    return redirect(url_for("dashboard"))


@app.route("/update_avatar", methods=["POST"])
def update_avatar():
    if not is_logged_in():
        return redirect(url_for("login_page"))
    avatar_color = normalize_hex_color(request.form.get("avatar_color", "#ffdac1"), "#ffdac1")
    avatar_face = request.form.get("avatar_face", "smile")
    avatar_clothes = request.form.get("avatar_clothes", "tshirt")
    avatar_pants = request.form.get("avatar_pants", "basic")
    avatar_arms = request.form.get("avatar_arms", "basic")
    avatar_wristwear = request.form.get("avatar_wristwear", "none")
    avatar_head_color = normalize_hex_color(request.form.get("avatar_head_color", avatar_color), avatar_color)
    avatar_torso_color = normalize_hex_color(request.form.get("avatar_torso_color", "#3b82f6"), "#3b82f6")
    avatar_arms_color = normalize_hex_color(request.form.get("avatar_arms_color", avatar_color), avatar_color)
    avatar_legs_color = normalize_hex_color(request.form.get("avatar_legs_color", "#1e293b"), "#1e293b")
    
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    
    owned_faces = user["owned_faces"].split(",") if user["owned_faces"] else []
    owned_clothes = user["owned_clothes"].split(",") if user["owned_clothes"] else []
    owned_pants = user["owned_pants"].split(",") if user["owned_pants"] else []
    owned_arms = user["owned_arms"].split(",") if user["owned_arms"] else []
    owned_wristwear = user["owned_wristwear"].split(",") if user["owned_wristwear"] else []
    
    if avatar_face not in owned_faces:
        avatar_face = "smile"
    if avatar_clothes not in owned_clothes:
        avatar_clothes = "tshirt"
    if avatar_pants not in owned_pants:
        avatar_pants = "basic"
    if avatar_arms not in owned_arms:
        avatar_arms = "basic"
    if avatar_wristwear not in owned_wristwear:
        avatar_wristwear = "none"
        
    conn.execute(
        """
        UPDATE users
        SET avatar_color = ?, avatar_face = ?, avatar_clothes = ?, avatar_pants = ?, avatar_arms = ?, avatar_wristwear = ?,
            avatar_head_color = ?, avatar_torso_color = ?, avatar_arms_color = ?, avatar_legs_color = ?
        WHERE id = ?
        """,
        (
            avatar_color,
            avatar_face,
            avatar_clothes,
            avatar_pants,
            avatar_arms,
            avatar_wristwear,
            avatar_head_color,
            avatar_torso_color,
            avatar_arms_color,
            avatar_legs_color,
            session["user_id"],
        ),
    )
    conn.commit()
    conn.close()
    flash("Avatar updated successfully!")
    return redirect(url_for("dashboard"))

@app.route("/buy_item", methods=["POST"])
def buy_item():
    if not is_logged_in():
        return redirect(url_for("login_page"))
    
    item_type = request.form.get("item_type")
    item_id = request.form.get("item_id")
    
    if item_type == "face" and item_id in FACES:
        price = FACES[item_id]["price"]
        name = FACES[item_id]["name"]
    elif item_type == "clothes" and item_id in CLOTHES:
        price = CLOTHES[item_id]["price"]
        name = CLOTHES[item_id]["name"]
    elif item_type == "pants" and item_id in PANTS:
        price = PANTS[item_id]["price"]
        name = PANTS[item_id]["name"]
    elif item_type == "arms" and item_id in ARMS:
        price = ARMS[item_id]["price"]
        name = ARMS[item_id]["name"]
    elif item_type == "wristwear" and item_id in WRISTWEAR:
        price = WRISTWEAR[item_id]["price"]
        name = WRISTWEAR[item_id]["name"]
    else:
        flash("Invalid item.")
        return redirect(url_for("dashboard"))
        
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    
    if user["pixels"] >= price:
        new_pixels = user["pixels"] - price
        if item_type == "face":
            owned = user["owned_faces"].split(",") if user["owned_faces"] else []
            if item_id not in owned:
                owned.append(item_id)
                conn.execute("UPDATE users SET pixels = ?, owned_faces = ? WHERE id = ?", (new_pixels, ",".join(owned), session["user_id"]))
                flash(f"Successfully bought {name}!")
            else:
                flash("You already own this face.")
        elif item_type == "clothes":
            owned = user["owned_clothes"].split(",") if user["owned_clothes"] else []
            if item_id not in owned:
                owned.append(item_id)
                conn.execute("UPDATE users SET pixels = ?, owned_clothes = ? WHERE id = ?", (new_pixels, ",".join(owned), session["user_id"]))
                flash(f"Successfully bought {name}!")
            else:
                flash("You already own these clothes.")
        elif item_type == "pants":
            owned = user["owned_pants"].split(",") if user["owned_pants"] else []
            if item_id not in owned:
                owned.append(item_id)
                conn.execute("UPDATE users SET pixels = ?, owned_pants = ? WHERE id = ?", (new_pixels, ",".join(owned), session["user_id"]))
                flash(f"Successfully bought {name}!")
            else:
                flash("You already own these pants.")
        elif item_type == "arms":
            owned = user["owned_arms"].split(",") if user["owned_arms"] else []
            if item_id not in owned:
                owned.append(item_id)
                conn.execute("UPDATE users SET pixels = ?, owned_arms = ? WHERE id = ?", (new_pixels, ",".join(owned), session["user_id"]))
                flash(f"Successfully bought {name}!")
            else:
                flash("You already own these arms.")
        elif item_type == "wristwear":
            owned = user["owned_wristwear"].split(",") if user["owned_wristwear"] else []
            if item_id not in owned:
                owned.append(item_id)
                conn.execute("UPDATE users SET pixels = ?, owned_wristwear = ? WHERE id = ?", (new_pixels, ",".join(owned), session["user_id"]))
                flash(f"Successfully bought {name}!")
            else:
                flash("You already own this wristwear.")
    else:
        flash("Not enough pixels!")
        
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/game")
def game_page():
    user = get_current_user()
    default_config = load_default_game_config()

    if user:
        session["play_pixel_last_award_at"] = int(time.time())
        display_name = user["display_name"]
        avatar_color = user["avatar_color"]
        avatar_face = user["avatar_face"]
        avatar_clothes = user["avatar_clothes"]
        avatar_pants = user["avatar_pants"]
        avatar_arms = user["avatar_arms"]
        avatar_wristwear = user["avatar_wristwear"]
        avatar_head_color = user["avatar_head_color"]
        avatar_torso_color = user["avatar_torso_color"]
        avatar_arms_color = user["avatar_arms_color"]
        avatar_legs_color = user["avatar_legs_color"]
    else:
        display_name = "Player"
        avatar_color = "#ffdac1"
        avatar_face = "smile"
        avatar_clothes = "tshirt"
        avatar_pants = "basic"
        avatar_arms = "basic"
        avatar_wristwear = "none"
        avatar_head_color = "#ffdac1"
        avatar_torso_color = "#3b82f6"
        avatar_arms_color = "#ffdac1"
        avatar_legs_color = "#1e293b"

    return render_template(
        "kill.html",
        display_name=display_name,
        avatar_color=avatar_color,
        avatar_face=avatar_face,
        avatar_clothes=avatar_clothes,
        avatar_pants=avatar_pants,
        avatar_arms=avatar_arms,
        avatar_wristwear=avatar_wristwear,
        avatar_head_color=avatar_head_color,
        avatar_torso_color=avatar_torso_color,
        avatar_arms_color=avatar_arms_color,
        avatar_legs_color=avatar_legs_color,
        game_config=default_config,
        game_title="Voxels",
        creator_name="Voxels",
        game_id="default",
        can_earn_pixels=bool(user),
        current_pixels=(user["pixels"] if user else 0),
    )


@app.route("/creator", methods=["GET", "POST"])
def game_creator():
    if not is_logged_in():
        return redirect(url_for("login_page"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        thumbnail_url = request.form.get("thumbnail_url", "").strip()
        config_text = request.form.get("config_json", "").strip()

        if not title:
            flash("Game title is required.")
            return redirect(url_for("game_creator"))

        try:
            config = parse_game_config(config_text)
        except (json.JSONDecodeError, ValueError) as error:
            flash(f"Invalid game JSON: {error}")
            return render_template(
                "creator.html",
                default_config_text=config_text or json.dumps(load_default_game_config(), indent=2),
                title_value=title,
                description_value=description,
                thumbnail_value=thumbnail_url,
            )

        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO games (creator_id, title, description, thumbnail_url, config_json, script_js, is_public)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (session["user_id"], title, description, thumbnail_url, json.dumps(config), ""),
        )
        conn.commit()
        game_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.close()
        flash("Game published! Share it from the feed.")
        return redirect(url_for("play_user_game", game_id=game_id))

    return render_template(
        "creator.html",
        default_config_text=json.dumps(load_default_game_config(), indent=2),
        title_value="",
        description_value="",
        thumbnail_value="",
    )


@app.route("/feed")
def feed_page():
    conn = get_db_connection()
    games = conn.execute(
        """
        SELECT games.id, games.title, games.description, games.thumbnail_url, games.created_at, users.username AS creator_username
        FROM games
        JOIN users ON users.id = games.creator_id
        WHERE games.is_public = 1
        ORDER BY games.id DESC
        """
    ).fetchall()
    conn.close()
    return render_template("games.html", games=games)


@app.route("/games")
def games_hub_redirect():
    return redirect(url_for("feed_page"))


@app.route("/my_games")
def my_games():
    if not is_logged_in():
        return redirect(url_for("login_page"))

    conn = get_db_connection()
    games = conn.execute(
        """
        SELECT games.id, games.title, games.description, games.thumbnail_url, games.created_at
        FROM games
        WHERE games.creator_id = ?
        ORDER BY games.id DESC
        """,
        (session["user_id"],),
    ).fetchall()
    conn.close()
    return render_template("my_games.html", games=games)


@app.route("/play/<int:game_id>")
def play_user_game(game_id):
    conn = get_db_connection()
    game = conn.execute(
        """
        SELECT games.*, users.username AS creator_username
        FROM games
        JOIN users ON users.id = games.creator_id
        WHERE games.id = ? AND games.is_public = 1
        """,
        (game_id,),
    ).fetchone()
    conn.close()

    if not game:
        flash("Game not found.")
        return redirect(url_for("feed_page"))

    user = get_current_user()
    if user:
        session["play_pixel_last_award_at"] = int(time.time())
        avatar_color = user["avatar_color"]
        avatar_face = user["avatar_face"]
        avatar_clothes = user["avatar_clothes"]
        avatar_pants = user["avatar_pants"]
        avatar_arms = user["avatar_arms"]
        avatar_wristwear = user["avatar_wristwear"]
        avatar_head_color = user["avatar_head_color"]
        avatar_torso_color = user["avatar_torso_color"]
        avatar_arms_color = user["avatar_arms_color"]
        avatar_legs_color = user["avatar_legs_color"]
        display_name = user["display_name"]
    else:
        avatar_color = "#ffdac1"
        avatar_face = "smile"
        avatar_clothes = "tshirt"
        avatar_pants = "basic"
        avatar_arms = "basic"
        avatar_wristwear = "none"
        avatar_head_color = "#ffdac1"
        avatar_torso_color = "#3b82f6"
        avatar_arms_color = "#ffdac1"
        avatar_legs_color = "#1e293b"
        display_name = "Guest"

    return render_template(
        "kill.html",
        display_name=display_name,
        avatar_color=avatar_color,
        avatar_face=avatar_face,
        avatar_clothes=avatar_clothes,
        avatar_pants=avatar_pants,
        avatar_arms=avatar_arms,
        avatar_wristwear=avatar_wristwear,
        avatar_head_color=avatar_head_color,
        avatar_torso_color=avatar_torso_color,
        avatar_arms_color=avatar_arms_color,
        avatar_legs_color=avatar_legs_color,
        game_config=json.loads(game["config_json"]),
        game_title=game["title"],
        creator_name=game["creator_username"],
        game_id=game_id,
        can_earn_pixels=bool(user),
        current_pixels=(user["pixels"] if user else 0),
    )


@app.route("/claim_play_pixels", methods=["POST"])
def claim_play_pixels():
    if not is_logged_in():
        return jsonify({"awarded": 0, "total_pixels": 0}), 401

    now_ts = int(time.time())
    last_award_ts = int(session.get("play_pixel_last_award_at", now_ts))
    elapsed_seconds = max(0, now_ts - last_award_ts)
    awarded_pixels = elapsed_seconds // 60

    conn = get_db_connection()
    user = conn.execute("SELECT pixels FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    total_pixels = user["pixels"] if user else 0

    if awarded_pixels > 0 and user:
        total_pixels = user["pixels"] + awarded_pixels
        conn.execute("UPDATE users SET pixels = ? WHERE id = ?", (total_pixels, session["user_id"]))
        conn.commit()
        session["play_pixel_last_award_at"] = last_award_ts + (awarded_pixels * 60)

    conn.close()
    return jsonify({"awarded": awarded_pixels, "total_pixels": total_pixels})


@app.route("/appeal", methods=["POST"])
def submit_appeal():
    if is_logged_in():
        flash("You are already logged in.")
        return redirect(url_for("dashboard"))

    username = request.form.get("username", "").strip()
    message = request.form.get("message", "").strip()

    if not username or not message:
        flash("Username and appeal message are required.")
        return redirect(url_for("login_page"))

    if len(message) > 1000:
        flash("Appeal message must be 1000 characters or fewer.")
        return redirect(url_for("login_page"))

    conn = get_db_connection()
    user = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if not user:
        conn.close()
        flash("No account found with that username.")
        return redirect(url_for("login_page"))

    existing = conn.execute(
        "SELECT id FROM appeals WHERE user_id = ? AND status = 'open'", (user["id"],)
    ).fetchone()
    if existing:
        conn.close()
        flash("An appeal for this account is already pending review.")
        return redirect(url_for("login_page"))

    conn.execute(
        "INSERT INTO appeals (user_id, message) VALUES (?, ?)",
        (user["id"], message),
    )
    conn.commit()
    conn.close()

    flash("Appeal submitted! We'll review it and get back to you.")
    return redirect(url_for("login_page"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ========== MOD PANEL ==========

def is_mod():
    if not is_logged_in():
        return False
    conn = get_db_connection()
    user = conn.execute("SELECT is_admin FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    return bool(user and user["is_admin"])


@app.route("/mod")
def mod_panel():
    if not is_mod():
        return redirect(url_for("login_page"))

    conn = get_db_connection()
    appeals = conn.execute(
        """
        SELECT appeals.*, users.username
        FROM appeals
        JOIN users ON users.id = appeals.user_id
        ORDER BY CASE appeals.status WHEN 'open' THEN 0 ELSE 1 END, appeals.created_at DESC
        """
    ).fetchall()
    users = conn.execute(
        "SELECT id, username, display_name, email, pixels, is_disabled, is_admin FROM users ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return render_template("mod.html", appeals=appeals, users=users)


@app.route("/mod/appeal/<int:appeal_id>/approve", methods=["POST"])
def mod_approve_appeal(appeal_id):
    if not is_mod():
        return redirect(url_for("login_page"))

    conn = get_db_connection()
    appeal = conn.execute("SELECT * FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
    if appeal:
        conn.execute("UPDATE appeals SET status = 'approved' WHERE id = ?", (appeal_id,))
        conn.execute("UPDATE users SET is_disabled = 0 WHERE id = ?", (appeal["user_id"],))
        conn.commit()
    conn.close()
    flash(f"Appeal #{appeal_id} approved — account restored.")
    return redirect(url_for("mod_panel"))


@app.route("/mod/appeal/<int:appeal_id>/deny", methods=["POST"])
def mod_deny_appeal(appeal_id):
    if not is_mod():
        return redirect(url_for("login_page"))

    conn = get_db_connection()
    conn.execute("UPDATE appeals SET status = 'denied' WHERE id = ?", (appeal_id,))
    conn.commit()
    conn.close()
    flash(f"Appeal #{appeal_id} denied.")
    return redirect(url_for("mod_panel"))


@app.route("/mod/user/<int:user_id>/disable", methods=["POST"])
def mod_disable_user(user_id):
    if not is_mod():
        return redirect(url_for("login_page"))
    if user_id == session["user_id"]:
        flash("You can't disable your own account.")
        return redirect(url_for("mod_panel"))

    conn = get_db_connection()
    conn.execute("UPDATE users SET is_disabled = 1 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash(f"User #{user_id} disabled.")
    return redirect(url_for("mod_panel"))


@app.route("/mod/user/<int:user_id>/enable", methods=["POST"])
def mod_enable_user(user_id):
    if not is_mod():
        return redirect(url_for("login_page"))

    conn = get_db_connection()
    conn.execute("UPDATE users SET is_disabled = 0 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash(f"User #{user_id} re-enabled.")
    return redirect(url_for("mod_panel"))


@app.route("/mod/user/<int:user_id>/toggle_admin", methods=["POST"])
def mod_toggle_admin(user_id):
    if not is_mod():
        return redirect(url_for("login_page"))
    if user_id == session["user_id"]:
        flash("You can't change your own admin status.")
        return redirect(url_for("mod_panel"))

    conn = get_db_connection()
    conn.execute("UPDATE users SET is_admin = NOT is_admin WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash(f"User #{user_id} admin status toggled.")
    return redirect(url_for("mod_panel"))


# ========== SOCKETIO MULTIPLAYER EVENTS ==========

@socketio.on('join_game')
def handle_join_game(data):
    """Player joins a game room"""
    game_id = str(data.get('game_id'))
    user_id = session.get('user_id')
    
    if not user_id:
        return
    
    join_room(game_id)
    
    # Get player avatar data
    conn = get_db_connection()
    user = conn.execute('SELECT display_name, avatar_head_color, avatar_torso_color, avatar_arms_color, avatar_legs_color, avatar_face, avatar_clothes, avatar_pants, avatar_arms, avatar_wristwear FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    
    if not user:
        return
    
    player_data = {
        'player_id': user_id,
        'display_name': user['display_name'],
        'avatar': {
            'head_color': user['avatar_head_color'],
            'torso_color': user['avatar_torso_color'],
            'arms_color': user['avatar_arms_color'],
            'legs_color': user['avatar_legs_color'],
            'face': user['avatar_face'],
            'clothes': user['avatar_clothes'],
            'pants': user['avatar_pants'],
            'arms': user['avatar_arms'],
            'wristwear': user['avatar_wristwear']
        },
        'position': {'x': 0, 'y': 0, 'z': 0},
        'rotation': 0
    }

    existing_players = []
    with presence_lock:
        previous = sid_presence.get(request.sid)
        if previous and previous.get('game_id') != game_id:
            previous_room = previous.get('game_id')
            previous_user = previous.get('user_id')
            previous_map = active_rooms.get(previous_room)
            if previous_map and previous_user in previous_map:
                previous_map.pop(previous_user, None)
                if not previous_map:
                    active_rooms.pop(previous_room, None)
            leave_room(previous_room)

        room_map = active_rooms.setdefault(game_id, {})
        existing_players = [player for uid, player in room_map.items() if uid != user_id]
        room_map[user_id] = player_data
        sid_presence[request.sid] = {'game_id': game_id, 'user_id': user_id}

    emit('room_players', {'players': existing_players})
    emit('player_joined', player_data, room=game_id, skip_sid=request.sid)
    

@socketio.on('leave_game')
def handle_leave_game(data):
    """Player leaves a game room"""
    game_id = str(data.get('game_id'))
    user_id = session.get('user_id')
    
    if not user_id:
        return

    removed = False
    with presence_lock:
        room_map = active_rooms.get(game_id)
        if room_map and user_id in room_map:
            room_map.pop(user_id, None)
            removed = True
            if not room_map:
                active_rooms.pop(game_id, None)
        sid_presence.pop(request.sid, None)

    leave_room(game_id)
    if removed:
        emit('player_left', {'player_id': user_id}, room=game_id)


@socketio.on('player_move')
def handle_player_move(data):
    """Broadcast player position to others in same game"""
    game_id = str(data.get('game_id'))
    user_id = session.get('user_id')
    
    if not user_id:
        return
    
    position = data.get('position', {})
    rotation = data.get('rotation', 0)

    with presence_lock:
        room_map = active_rooms.get(game_id)
        if room_map and user_id in room_map:
            room_map[user_id]['position'] = {
                'x': position.get('x', 0),
                'y': position.get('y', 0),
                'z': position.get('z', 0),
            }
            room_map[user_id]['rotation'] = rotation
    
    # Broadcast to others in room
    emit('player_moved', {
        'player_id': user_id,
        'position': position,
        'rotation': rotation
    }, room=game_id, skip_sid=request.sid)


@socketio.on('chat_message')
def handle_chat_message(data):
    game_id = str(data.get('game_id'))
    user_id = session.get('user_id')

    if not user_id:
        return

    text = str(data.get('message', '')).strip()
    if not text:
        return
    if len(text) > 220:
        text = text[:220]

    with presence_lock:
        room_map = active_rooms.get(game_id)
        presence = sid_presence.get(request.sid)
        if not room_map or user_id not in room_map:
            return
        if not presence or presence.get('game_id') != game_id:
            return
        sender_name = room_map[user_id]['display_name']

    emit('chat_message', {
        'player_id': user_id,
        'display_name': sender_name,
        'message': text,
    }, room=game_id)


@socketio.on('disconnect')
def handle_disconnect():
    presence = None
    with presence_lock:
        presence = sid_presence.pop(request.sid, None)
        if not presence:
            return

        game_id = presence.get('game_id')
        user_id = presence.get('user_id')
        room_map = active_rooms.get(game_id)
        if room_map and user_id in room_map:
            room_map.pop(user_id, None)
            if not room_map:
                active_rooms.pop(game_id, None)

    if presence:
        emit('player_left', {'player_id': presence.get('user_id')}, room=presence.get('game_id'))


init_db()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
