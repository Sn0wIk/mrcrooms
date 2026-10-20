import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import create_app
from media import parse_vk


class RoomsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "test.sqlite3")
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test", "DATABASE": self.path})
        self.owner, self.guest = self.client(), self.client()
        response = self.post(self.owner, "/api/rooms", {"title": "Вечер", "url": ""})
        self.assertEqual(response.status_code, 201)
        self.code = response.json["url"].split("/")[-1]
        self.base = f"/api/rooms/{self.code}"
        self.a = self.post(self.owner, self.base + "/join", {"name": "Аня"}).json["id"]
        self.b = self.post(self.guest, self.base + "/join", {"name": "Боря"}).json["id"]

    def client(self):
        client = self.app.test_client()
        client.get("/")
        return client

    def post(self, client, path, payload):
        with client.session_transaction() as session:
            token = session["csrf"]
        return client.post(path, json=payload, headers={"X-CSRF-Token": token})

    def poll(self, client, member, extra=""):
        return client.get(f"{self.base}/poll?member={member}{extra}")

    def test_vk_links(self):
        for url in (
            "https://vk.com/video-123_456",
            "https://m.vk.ru/video-123_456?list=abc",
            "https://vkvideo.ru/video-123_456",
            "https://vk.com/videos-123?z=video-123_456%2Fpl_-123",
            "https://vk.ru/video_ext.php?oid=-123&id=456&hash=abc123",
        ):
            with self.subTest(url=url):
                media = parse_vk(url)
                self.assertIn("oid=-123&id=456", media["embed"])
                self.assertIn("js_api=1", media["embed"])
        self.assertIn("hash=abc123", parse_vk(url)["embed"])

    def test_vk_rejects_other_hosts_and_malformed_input(self):
        for url in (
            "https://vk.com.evil.test/video-123_456",
            "https://vk.com@evil.test/video-123_456",
            "https://user@vk.com/video-123_456",
            "http://vk.com/video-123_456",
            "https://vk.com:8443/video-123_456",
            "https://127.0.0.1/video-123_456",
            "https://vk.com/feed", "javascript:alert(1)", None,
            "https://vk.com/video_ext.php?oid=hi&id=1",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_vk(url)

    def test_chat_presence_history_and_xss_escaping(self):
        body = '<img src=x onerror=alert(1)>'
        result = self.post(self.owner, self.base + "/chat", {"member": self.a, "body": body})
        self.assertEqual(result.status_code, 200)
        state = self.poll(self.guest, self.b).json
        self.assertEqual(len(state["members"]), 2)
        self.assertEqual(state["messages"][0]["body"], body)
        self.assertEqual(state["messages"][0]["name"], "Аня")
        last = state["messages"][-1]["id"]
        self.assertEqual(self.poll(self.guest, self.b, f"&after={last}").json["messages"], [])
        self.assertEqual(self.post(self.owner, self.base + "/chat", {"member": self.a, "body": " "}).status_code, 400)

    def test_csrf_and_member_impersonation(self):
        self.assertEqual(self.owner.post(self.base + "/chat", json={}).status_code, 403)
        self.assertEqual(self.post(self.guest, self.base + "/chat", {"member": self.a, "body": "x"}).status_code, 403)
        self.assertEqual(self.poll(self.guest, self.a).status_code, 403)
        self.assertEqual(self.post(self.guest, self.base + "/voice", {"member": self.a, "enabled": True, "muted": False}).status_code, 403)

    def test_owner_controls_and_advancing_position(self):
        url = "https://vkvideo.ru/video-123_456"
        self.assertEqual(self.post(self.guest, self.base + "/media", {"url": url}).status_code, 403)
        self.assertEqual(self.post(self.owner, self.base + "/media", {"url": url}).status_code, 200)
        self.assertEqual(self.post(self.guest, self.base + "/control", {"action": "play", "position": 0}).status_code, 403)
        self.assertEqual(self.post(self.owner, self.base + "/control", {"action": "play", "position": 20}).status_code, 200)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE rooms SET updated=? WHERE code=?", (time.time() - 10, self.code))
        state = self.poll(self.guest, self.b).json["state"]
        self.assertTrue(state["playing"])
        self.assertAlmostEqual(state["position"], 30, delta=1)
        self.post(self.owner, self.base + "/control", {"action": "pause", "position": 42})
        state = self.poll(self.guest, self.b).json["state"]
        self.assertFalse(state["playing"])
        self.assertEqual(state["position"], 42)
        for position in (-1, 999999, True, "zero", None, float("nan")):
            self.assertEqual(self.post(self.owner, self.base + "/control", {"action": "seek", "position": position}).status_code, 400)

    def test_signal_delivery_ack_and_room_isolation(self):
        for client, member in ((self.owner, self.a), (self.guest, self.b)):
            self.post(client, self.base + "/voice", {"member": member, "enabled": True, "muted": False})
        payload = {"member": self.a, "to": self.b, "payload": {"type": "offer", "sdp": "test"}}
        self.assertEqual(self.post(self.owner, self.base + "/signal", payload).status_code, 200)
        first = self.poll(self.guest, self.b).json["signals"]
        self.assertEqual(len(first), 1)
        self.assertEqual(first, self.poll(self.guest, self.b).json["signals"])
        self.assertEqual(self.poll(self.owner, self.a).json["signals"], [])
        self.assertEqual(self.poll(self.guest, self.b, f'&signal_after={first[0]["id"]}').json["signals"], [])
        other_room = self.post(self.owner, "/api/rooms", {"title": "Другая"}).json["url"].split("/")[-1]
        outsider = self.post(self.guest, f"/api/rooms/{other_room}/join", {"name": "Друг"}).json["id"]
        payload["to"] = outsider
        self.assertEqual(self.post(self.owner, self.base + "/signal", payload).status_code, 409)
        self.assertEqual(self.poll(self.guest, outsider).status_code, 403)

    def test_voice_leave_and_expired_membership(self):
        self.post(self.owner, self.base + "/voice", {"member": self.a, "enabled": True, "muted": True})
        own = next(item for item in self.poll(self.guest, self.b).json["members"] if item["id"] == self.a)
        self.assertTrue(own["voice"])
        self.assertTrue(own["muted"])
        self.assertEqual(self.post(self.owner, self.base + "/leave", {"member": self.a}).status_code, 200)
        self.assertEqual(self.poll(self.owner, self.a).status_code, 403)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE members SET seen=0 WHERE id=?", (self.b,))
        self.assertEqual(self.poll(self.guest, self.b).status_code, 403)

    def test_room_capacity(self):
        for index in range(6):
            self.assertEqual(self.post(self.owner, self.base + "/join", {"name": str(index)}).status_code, 200)
        self.assertEqual(self.post(self.owner, self.base + "/join", {"name": "Лишний"}).status_code, 409)
        self.post(self.guest, self.base + "/leave", {"member": self.b})
        self.assertEqual(self.post(self.owner, self.base + "/join", {"name": "Теперь можно"}).status_code, 200)

    def test_rate_limit_and_invalid_cursors(self):
        for _ in range(20):
            self.assertEqual(self.post(self.owner, self.base + "/chat", {"member": self.a, "body": "x"}).status_code, 200)
        self.assertEqual(self.post(self.owner, self.base + "/chat", {"member": self.a, "body": "x"}).status_code, 429)
        for cursor in ("bad", "-1", "999999999999999999999999999"):
            self.assertEqual(self.poll(self.guest, self.b, f"&after={cursor}").status_code, 400)

    def test_restart_keeps_room_and_history(self):
        self.post(self.owner, self.base + "/chat", {"member": self.a, "body": "Сохрани меня"})
        restarted = create_app({"TESTING": True, "SECRET_KEY": "test", "DATABASE": self.path})
        self.assertEqual(restarted.test_client().get(f"/r/{self.code}").status_code, 200)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT body FROM messages").fetchone()[0], "Сохрани меня")

    def test_voice_diagnostics_are_scoped_and_do_not_log_raw_payload(self):
        payload = {
            "member": self.a, "event": "failed", "connection": "failed",
            "ice": "failed", "local_types": ["host", "srflx", "PRIVATE_ADDRESS"],
            "remote_types": ["host"], "sdp": "PRIVATE_SDP", "credential": "PRIVATE_KEY",
        }
        self.assertEqual(self.post(self.guest, self.base + "/voice-diagnostics", payload).status_code, 403)
        with self.assertLogs(self.app.logger, level="WARNING") as captured:
            response = self.post(self.owner, self.base + "/voice-diagnostics", payload)
        self.assertEqual(response.status_code, 200)
        log = " ".join(captured.output)
        self.assertIn('"turn_configured": false', log)
        self.assertIn('"ice": "failed"', log)
        for private in ("PRIVATE_ADDRESS", "PRIVATE_SDP", "PRIVATE_KEY"):
            self.assertNotIn(private, log)


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / "persistent" / "data"
        self.environment = patch.dict("os.environ", {
            "MRC_ENV": "development",
            "MRC_DATA_DIR": str(self.data_dir),
            "SECRET_KEY": "",
            "ICE_SERVERS_JSON": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_fresh_volume_with_environment_secret_and_restart(self):
        with patch.dict("os.environ", {
            "MRC_ENV": "production", "SECRET_KEY": "test-persistent-secret-" * 3,
        }):
            app = create_app({"TESTING": True})
            client = app.test_client()
            client.get("/", base_url="https://rooms.example.com")
            with client.session_transaction(base_url="https://rooms.example.com") as session:
                token = session["csrf"]
            created = client.post(
                "/api/rooms", json={"title": "Persistent room"},
                base_url="https://rooms.example.com",
                headers={"X-CSRF-Token": token},
            )
            self.assertEqual(created.status_code, 201)
            restarted = create_app({"TESTING": True})
            self.assertEqual(app.secret_key, restarted.secret_key)
            self.assertEqual(restarted.test_client().get(created.json["url"]).status_code, 200)
            self.assertTrue((self.data_dir / "rooms.sqlite3").exists())
            self.assertFalse((self.data_dir / "session.key").exists())
            health = client.get("/health")
            self.assertIn("max-age=", health.headers["Strict-Transport-Security"])
            response = app.test_client().get("/")
            self.assertIn("Secure", response.headers["Set-Cookie"])

    def test_production_requires_secret(self):
        with patch.dict("os.environ", {"MRC_ENV": "production"}):
            with self.assertRaisesRegex(ValueError, "SECRET_KEY"):
                create_app()
        self.assertFalse(self.data_dir.exists())

    def test_development_key_persists(self):
        app = create_app({"TESTING": True})
        self.assertEqual(app.secret_key, create_app({"TESTING": True}).secret_key)
        key = self.data_dir / "session.key"
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(app.config["ICE_SERVERS"][0]["urls"], "stun:stun.l.google.com:19302")


if __name__ == "__main__":
    unittest.main()
