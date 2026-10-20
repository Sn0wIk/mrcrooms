import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import time

from flask import Flask, abort, g, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException

from media import parse_vk

BASE = Path(__file__).resolve().parent
PRESENCE_SECONDS = 35
MAX_MEMBERS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    code TEXT PRIMARY KEY, title TEXT NOT NULL, owner TEXT NOT NULL,
    media TEXT, playing INTEGER NOT NULL DEFAULT 0,
    position REAL NOT NULL DEFAULT 0, updated REAL NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    id TEXT PRIMARY KEY, room TEXT NOT NULL REFERENCES rooms(code),
    identity TEXT NOT NULL, name TEXT NOT NULL, seen REAL NOT NULL,
    voice INTEGER NOT NULL DEFAULT 0, muted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS members_room ON members(room, seen);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT NOT NULL REFERENCES rooms(code), member TEXT NOT NULL,
    name TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_room ON messages(room, id);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT NOT NULL REFERENCES rooms(code), sender TEXT NOT NULL,
    recipient TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS signals_recipient ON signals(recipient, id);
CREATE TABLE IF NOT EXISTS limits (
    key TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL
);
"""


def create_app(config=None):
    environment = os.getenv("MRC_ENV", "development")
    if environment not in {"development", "production"}:
        raise ValueError("MRC_ENV must be development or production.")
    data_dir = Path(os.getenv("MRC_DATA_DIR", str(BASE / "instance"))).resolve()
    app = Flask(__name__)
    app.config.update(
        DATA_DIR=str(data_dir),
        DATABASE=str(data_dir / "rooms.sqlite3"),
        PRODUCTION=environment == "production",
        SECRET_KEY=os.getenv("SECRET_KEY"),
        MAX_CONTENT_LENGTH=32_768,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=environment == "production" or os.getenv("COOKIE_SECURE") == "1",
        ICE_SERVERS=json.loads(
            os.getenv("ICE_SERVERS_JSON") or '[{"urls":"stun:stun.l.google.com:19302"}]'
        ),
    )
    if config:
        app.config.update(config)
    if app.config["PRODUCTION"]:
        secret = app.config.get("SECRET_KEY")
        if not isinstance(secret, str) or len(secret) < 32:
            raise ValueError("Production requires a persistent SECRET_KEY of at least 32 characters.")
        if not app.config["SESSION_COOKIE_SECURE"]:
            raise ValueError("Production requires secure session cookies and HTTPS.")
    # Create storage even when SECRET_KEY comes from the environment.
    # This is essential for the first start on a fresh persistent volume.
    Path(app.config["DATABASE"]).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not app.config.get("SECRET_KEY"):
        instance = Path(app.config["DATA_DIR"])
        instance.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_path = instance / "session.key"
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_hex(32))
        app.config["SECRET_KEY"] = key_path.read_text().strip()

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"], timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON")
        return g.db

    with app.app_context():
        db().execute("PRAGMA journal_mode=WAL")
        db().executescript(SCHEMA)
        db().close()

    @app.teardown_appcontext
    def close_db(_error):
        conn = g.pop("db", None)
        if conn:
            conn.close()

    @app.before_request
    def protect():
        if request.endpoint == "static":
            return
        if "identity" not in session:
            session["identity"] = secrets.token_urlsafe(24)
            session["csrf"] = secrets.token_urlsafe(24)
        if request.method == "POST":
            token = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(token, session["csrf"]):
                abort(403, "Сессия изменилась. Обновите страницу.")
            if not request.is_json:
                abort(415, "Ожидался JSON.")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "microphone=(self), camera=()"
        if app.config["PRODUCTION"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return render_template("error.html", message=error.description), error.code

    def data():
        value = request.get_json()
        if not isinstance(value, dict):
            abort(400, "Некорректные данные.")
        return value

    def text(value, max_length, label):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
            abort(400, f"{label}: от 1 до {max_length} символов.")
        return value.strip()

    def limit(key, maximum, period):
        now = time.time()
        key = session["identity"] + ":" + key
        # One atomic UPSERT also serializes concurrent requests.
        row = db().execute(
            """INSERT INTO limits VALUES (?, ?, 1)
               ON CONFLICT(key) DO UPDATE SET
               count=CASE WHEN start < ? THEN 1 ELSE count+1 END,
               start=CASE WHEN start < ? THEN excluded.start ELSE start END
               RETURNING count""",
            (key, now, now - period, now - period),
        ).fetchone()
        db().commit()
        if row["count"] > maximum:
            abort(429, "Слишком часто. Подождите немного.")

    def room_for(code):
        row = db().execute("SELECT * FROM rooms WHERE code=?", (code,)).fetchone()
        if row is None:
            abort(404, "Комната не найдена. Проверьте ссылку приглашения.")
        return row

    def member_for(code, member_id):
        if not isinstance(member_id, str):
            abort(403, "Войдите в комнату.")
        row = db().execute(
            "SELECT * FROM members WHERE id=? AND room=? AND identity=?",
            (member_id, code, session["identity"]),
        ).fetchone()
        if row is None or row["seen"] < time.time() - PRESENCE_SECONDS:
            abort(403, "Подключение истекло. Войдите в комнату снова.")
        return row

    def owner_only(code):
        room = room_for(code)
        if room["owner"] != session["identity"]:
            abort(403, "Управлять видео может только создатель комнаты.")
        return room

    def cursor(name):
        try:
            value = int(request.args.get(name, "0"))
            if not 0 <= value <= 9223372036854775807:
                raise ValueError
            return value
        except ValueError:
            abort(400, "Некорректный курсор.")

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/r/<code>")
    def room_page(code):
        room = room_for(code)
        return render_template(
            "room.html", room=room, owner=room["owner"] == session["identity"],
            ice_servers=app.config["ICE_SERVERS"],
        )

    @app.get("/health")
    def health():
        db().execute("SELECT 1").fetchone()
        return jsonify(ok=True)

    @app.post("/api/parse")
    def parse():
        limit("parse", 60, 60)
        try:
            return jsonify(parse_vk(data().get("url")))
        except ValueError as error:
            abort(400, str(error))

    @app.post("/api/rooms")
    def create_room():
        limit("create", 10, 3600)
        payload = data()
        title = text(payload.get("title"), 80, "Название комнаты")
        media = None
        if payload.get("url"):
            try:
                media = parse_vk(payload["url"])
            except ValueError as error:
                abort(400, str(error))
        code = secrets.token_urlsafe(12)
        now = time.time()
        db().execute(
            "INSERT INTO rooms(code,title,owner,media,updated,created) VALUES(?,?,?,?,?,?)",
            (code, title, session["identity"], json.dumps(media) if media else None, now, now),
        )
        db().commit()
        return jsonify(url=f"/r/{code}"), 201

    @app.post("/api/rooms/<code>/join")
    def join(code):
        room_for(code)
        limit("join", 30, 60)
        name = text(data().get("name"), 32, "Имя")
        now = time.time()
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM signals WHERE created < ?", (now - 120,))
        conn.execute("DELETE FROM members WHERE seen < ?", (now - PRESENCE_SECONDS,))
        conn.execute("DELETE FROM limits WHERE start < ?", (now - 86400,))
        count = conn.execute("SELECT COUNT(*) FROM members WHERE room=?", (code,)).fetchone()[0]
        if count >= MAX_MEMBERS:
            conn.rollback()
            abort(409, "Комната заполнена: максимум 8 участников.")
        member_id = secrets.token_urlsafe(18)
        conn.execute(
            "INSERT INTO members(id,room,identity,name,seen) VALUES(?,?,?,?,?)",
            (member_id, code, session["identity"], name, now),
        )
        conn.commit()
        return jsonify(id=member_id, name=name)

    @app.get("/api/rooms/<code>/poll")
    def poll(code):
        room = room_for(code)
        member = member_for(code, request.args.get("member"))
        after, signal_after = cursor("after"), cursor("signal_after")
        now = time.time()
        db().execute("UPDATE members SET seen=? WHERE id=?", (now, member["id"]))
        # Reading signals is repeatable until the client acknowledges via its cursor.
        db().execute(
            "DELETE FROM signals WHERE recipient=? AND id<=?", (member["id"], signal_after)
        )
        db().commit()
        members = db().execute(
            """SELECT id,name,voice,muted FROM members WHERE room=? AND seen>=?
               ORDER BY id""", (code, now - PRESENCE_SECONDS)
        ).fetchall()
        messages = db().execute(
            """SELECT id,member,name,body,created FROM messages WHERE room=? AND id>?
               ORDER BY id DESC LIMIT 100""", (code, after)
        ).fetchall()
        signals = db().execute(
            """SELECT id,sender,payload FROM signals
               WHERE room=? AND recipient=? AND id>? AND created>=?
               ORDER BY id LIMIT 100""", (code, member["id"], signal_after, now - 120)
        ).fetchall()
        return jsonify(
            members=[dict(row) for row in members],
            messages=[dict(row) for row in reversed(messages)],
            signals=[dict(row, payload=json.loads(row["payload"])) for row in signals],
            state={
                "media": json.loads(room["media"]) if room["media"] else None,
                "playing": bool(room["playing"]),
                "position": room["position"] + (max(0, now - room["updated"]) if room["playing"] else 0),
                "revision": room["revision"],
            },
        )

    @app.post("/api/rooms/<code>/chat")
    def chat(code):
        payload = data()
        member = member_for(code, payload.get("member"))
        body = text(payload.get("body"), 2000, "Сообщение")
        limit("chat", 20, 10)
        db().execute(
            "INSERT INTO messages(room,member,name,body,created) VALUES(?,?,?,?,?)",
            (code, member["id"], member["name"], body, time.time()),
        )
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/rooms/<code>/media")
    def change_media(code):
        owner_only(code)
        limit("media", 30, 60)
        try:
            media = parse_vk(data().get("url"))
        except ValueError as error:
            abort(400, str(error))
        db().execute(
            """UPDATE rooms SET media=?,playing=0,position=0,updated=?,
               revision=revision+1 WHERE code=?""", (json.dumps(media), time.time(), code)
        )
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/rooms/<code>/control")
    def control(code):
        room = owner_only(code)
        limit("control", 90, 60)
        payload = data()
        action, position = payload.get("action"), payload.get("position")
        if action not in ("play", "pause", "seek") or isinstance(position, bool):
            abort(400, "Неизвестная команда.")
        if not isinstance(position, (int, float)) or not math.isfinite(position) or not 0 <= position <= 604800:
            abort(400, "Некорректная позиция видео.")
        if not room["media"]:
            abort(400, "Сначала добавьте видео.")
        playing = room["playing"] if action == "seek" else int(action == "play")
        db().execute(
            """UPDATE rooms SET playing=?,position=?,updated=?,revision=revision+1
               WHERE code=?""", (playing, position, time.time(), code)
        )
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/rooms/<code>/voice")
    def voice(code):
        payload = data()
        member = member_for(code, payload.get("member"))
        if type(payload.get("enabled")) is not bool or type(payload.get("muted")) is not bool:
            abort(400, "Некорректное состояние микрофона.")
        db().execute(
            "UPDATE members SET voice=?,muted=? WHERE id=?",
            (payload["enabled"], payload["muted"], member["id"]),
        )
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/rooms/<code>/signal")
    def signal(code):
        payload = data()
        member = member_for(code, payload.get("member"))
        limit("signal", 600, 60)
        target = payload.get("to")
        if not isinstance(target, str):
            abort(400, "Получатель не указан.")
        recipient = db().execute(
            "SELECT * FROM members WHERE id=? AND room=? AND seen>=?",
            (target, code, time.time() - PRESENCE_SECONDS),
        ).fetchone()
        if not member["voice"] or not recipient or not recipient["voice"] or target == member["id"]:
            abort(409, "Участник уже вышел из голосового чата.")
        value = payload.get("payload")
        if not isinstance(value, dict) or value.get("type") not in ("offer", "answer", "candidate"):
            abort(400, "Некорректный сигнал.")
        encoded = json.dumps(value)
        if len(encoded) > 20000:
            abort(400, "Сигнал слишком большой.")
        db().execute(
            "INSERT INTO signals(room,sender,recipient,payload,created) VALUES(?,?,?,?,?)",
            (code, member["id"], target, encoded, time.time()),
        )
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/rooms/<code>/voice-diagnostics")
    def voice_diagnostics(code):
        payload = data()
        member = member_for(code, payload.get("member"))
        limit("voice-diagnostics", 30, 60)
        # Never log SDP, candidate addresses, TURN credentials or arbitrary text.
        allowed = {
            "event": {"failed", "ice-error", "signal-error", "manual"},
            "connection": {"new", "connecting", "connected", "disconnected", "failed", "closed"},
            "ice": {"new", "checking", "connected", "completed", "disconnected", "failed", "closed"},
            "gathering": {"new", "gathering", "complete"},
            "signaling": {"stable", "have-local-offer", "have-remote-offer", "have-local-pranswer", "have-remote-pranswer", "closed"},
            "phase": {"candidate", "offer", "receive"},
        }
        record = {"room": code, "member": member["id"][:8]}
        for key, values in allowed.items():
            value = payload.get(key)
            if isinstance(value, str) and value in values:
                record[key] = value
        if "event" not in record:
            abort(400, "Неизвестное событие диагностики.")
        for key in ("local_types", "remote_types"):
            values = payload.get(key, [])
            if isinstance(values, list):
                record[key] = sorted({
                    value for value in values
                    if isinstance(value, str) and value in {"host", "srflx", "prflx", "relay"}
                })
        if type(payload.get("error_code")) is int and 0 <= payload["error_code"] <= 999:
            record["error_code"] = payload["error_code"]
        record["turn_configured"] = any(
            url.startswith(("turn:", "turns:"))
            for server in app.config["ICE_SERVERS"]
            for url in ([server["urls"]] if isinstance(server["urls"], str) else server["urls"])
        )
        app.logger.warning("voice_diagnostic %s", json.dumps(record, ensure_ascii=True))
        return jsonify(ok=True)

    @app.post("/api/rooms/<code>/leave")
    def leave(code):
        payload = data()
        member = member_for(code, payload.get("member"))
        db().execute("DELETE FROM members WHERE id=?", (member["id"],))
        db().execute("DELETE FROM signals WHERE sender=? OR recipient=?", (member["id"], member["id"]))
        db().commit()
        return jsonify(ok=True)

    return app


if __name__ == "__main__":
    from waitress import serve

    app = create_app()
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8010"))
    print(f"MRC Rooms → http://{host}:{port}", flush=True)
    serve(app, host=host, port=port, threads=12)
