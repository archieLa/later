import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from app import create_app


class LaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "later.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.database})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, data, method="POST", client=None):
        return (client or self.client).open(path, method=method, json=data, headers={"X-Later": "1"})

    def add(self, url="https://example.com/article", description="A good read"):
        response = self.write("/api/items", {"url": url, "description": description})
        self.assertEqual(response.status_code, 201)
        return response.json["item"]

    def pop(self, mode="unread"):
        return self.write("/api/pop", {"mode": mode})

    def test_empty_state_and_views(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Pick a link", response.data)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(self.client.get("/api/state").json, {"counts": {"total": 0, "unread": 0, "starred": 0}, "last": None})
        for mode in ("unread", "all", "starred"):
            self.assertEqual(self.pop(mode).status_code, 404)

    def test_add_trim_and_optional_description(self):
        response = self.write("/api/items", {"url": " https://www.youtube.com/watch?v=abc "})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["item"]["description"], "")
        self.assertEqual(response.json["item"]["url"], "https://www.youtube.com/watch?v=abc")
        self.assertFalse(response.json["item"]["is_read"])

    def test_invalid_urls_and_fields(self):
        for url in ("", "example.com", "javascript:alert(1)", "file:///tmp/x", "https://", "https://[bad", "https://a:bad", "https://a:0", "https://user:pass@example.com", "https://a/white space", "https://a/\nx", "https://a/" + "x" * 4096, None, 42):
            self.assertEqual(self.write("/api/items", {"url": url}).status_code, 400, repr(url))
        for description in (None, 9, "x" * 2001):
            self.assertEqual(self.write("/api/items", {"url": "https://example.com", "description": description}).status_code, 400)

    def test_pop_marks_read_and_unread_does_not_repeat(self):
        ids = {self.add(f"https://example.com/{n}")["id"] for n in range(5)}
        picked = [self.pop().json["item"] for _ in range(5)]
        self.assertEqual({entry["id"] for entry in picked}, ids)
        self.assertTrue(all(entry["is_read"] for entry in picked))
        self.assertEqual(self.pop().status_code, 404)
        self.assertEqual(self.client.get("/api/state").json["counts"]["unread"], 0)

    def test_all_includes_read_and_last_survives_empty_pop(self):
        entry = self.add()
        self.pop()
        self.assertEqual(self.pop("all").json["item"]["id"], entry["id"])
        self.pop()
        self.assertEqual(self.client.get("/api/state").json["last"]["id"], entry["id"])

    def test_star_unstar_browse_and_random(self):
        first = self.add()
        self.add("https://example.org")
        self.pop()
        response = self.write(f"/api/items/{first['id']}/star", {"starred": True}, "PATCH")
        self.assertTrue(response.json["item"]["starred"])
        self.assertEqual([entry["id"] for entry in self.client.get("/api/starred").json["items"]], [first["id"]])
        picked = self.pop("starred").json["item"]
        self.assertEqual(picked["id"], first["id"])
        self.assertTrue(picked["is_read"])
        self.assertTrue(self.client.get("/api/state").json["last"]["starred"])
        self.write(f"/api/items/{first['id']}/star", {"starred": False}, "PATCH")
        self.assertEqual(self.client.get("/api/starred").json["items"], [])
        self.assertFalse(self.client.get("/api/state").json["last"]["starred"])

    def test_persistent_data_and_last(self):
        first = self.add()
        self.pop()
        self.write(f"/api/items/{first['id']}/star", {"starred": True}, "PATCH")
        restarted = create_app({"TESTING": True, "DATABASE": self.database}).test_client()
        state = restarted.get("/api/state").json
        self.assertEqual(state["counts"], {"total": 1, "unread": 0, "starred": 1})
        self.assertEqual(state["last"]["id"], first["id"])

    def test_invalid_requests(self):
        for mode in ("invalid", [], None):
            self.assertEqual(self.pop(mode).status_code, 400)
        for data in ({}, {"starred": 1}, {"starred": "true"}):
            self.assertEqual(self.write("/api/items/1/star", data, "PATCH").status_code, 400)
        self.assertEqual(self.write("/api/items/999/star", {"starred": True}, "PATCH").status_code, 404)
        self.assertEqual(self.write("/api/pop", []).status_code, 400)
        self.assertEqual(self.client.post("/api/pop", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/pop", data="{}", headers={"X-Later": "1"}).status_code, 403)
        self.assertEqual(self.client.post("/api/pop", data="{bad", content_type="application/json", headers={"X-Later": "1"}).status_code, 400)
        self.assertEqual(self.write("/api/items", {"url": "https://example.com", "description": "x" * 20000}).status_code, 413)

    def test_description_is_data_and_sql_is_parameterized(self):
        entry = self.add("https://example.com/?q='", "<script>alert(1)</script>'); DROP TABLE items;--")
        self.assertEqual(self.pop().json["item"]["description"], entry["description"])
        self.assertEqual(self.client.get("/api/state").json["counts"]["total"], 1)

    def test_save_for_later_again_preserves_link_favorite_and_last(self):
        entry = self.add(description="A long video")
        self.pop()
        self.write(f"/api/items/{entry['id']}/star", {"starred": True}, "PATCH")
        response = self.write(f"/api/items/{entry['id']}/unread", {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["item"]["is_read"])
        self.assertTrue(response.json["item"]["starred"])
        self.assertEqual(response.json["item"]["description"], "A long video")
        state = self.client.get("/api/state").json
        self.assertEqual(state["counts"], {"total": 1, "unread": 1, "starred": 1})
        self.assertEqual(state["last"]["id"], entry["id"])
        self.assertFalse(state["last"]["is_read"])
        # Repeating the request is harmless and does not duplicate the link.
        self.assertEqual(self.write(f"/api/items/{entry['id']}/unread", {}).status_code, 200)
        restarted = create_app({"TESTING": True, "DATABASE": self.database}).test_client()
        self.assertFalse(restarted.get("/api/state").json["last"]["is_read"])
        picked = self.pop().json["item"]
        self.assertEqual(picked["id"], entry["id"])
        self.assertTrue(picked["is_read"])
        self.assertTrue(picked["starred"])

    def test_save_for_later_again_missing_link_and_write_protection(self):
        self.assertEqual(self.write("/api/items/999/unread", {}).status_code, 404)
        self.assertEqual(self.client.post("/api/items/1/unread", json={}).status_code, 403)

    def test_backup_roundtrip_preserves_everything(self):
        first = self.add(description="A favorite — café")
        self.pop()
        self.write(f"/api/items/{first['id']}/star", {"starred": True}, "PATCH")
        self.add("https://example.org/video", "")
        response = self.client.get("/api/backup")
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="later-backup-', response.headers["Content-Disposition"])
        self.assertEqual(response.mimetype, "application/json")
        backup = response.json
        self.assertEqual(backup["last_item_id"], first["id"])
        self.assertTrue(backup["items"][0]["starred"])
        self.assertTrue(backup["items"][0]["is_read"])
        self.assertFalse(backup["items"][1]["is_read"])
        self.add("https://extra.example.com")
        self.assertEqual(self.write("/api/restore", backup).json, {"restored": 2})
        self.assertEqual(self.client.get("/api/backup").json, backup)
        restarted = create_app({"TESTING": True, "DATABASE": self.database}).test_client()
        self.assertEqual(restarted.get("/api/backup").json, backup)
        self.assertEqual(self.pop().json["item"]["url"], "https://example.org/video")
        self.assertGreater(self.add("https://new.example.com")["id"], backup["items"][-1]["id"])

    def test_restore_empty_backup_clears_queue_and_last(self):
        empty = self.client.get("/api/backup").json
        self.add()
        self.pop()
        self.assertEqual(self.write("/api/restore", empty).status_code, 200)
        self.assertEqual(self.client.get("/api/backup").json, empty)
        self.assertIsNone(self.client.get("/api/state").json["last"])

    def test_restore_invalid_files_leave_existing_data_unchanged(self):
        self.add()
        self.pop()
        backup = self.client.get("/api/backup").json
        variants = [{}, {**backup, "app": "Other"}, {**backup, "version": 2},
                    {**backup, "version": True}, {**backup, "items": None},
                    {**backup, "last_item_id": 999}, {**backup, "last_item_id": True},
                    {**backup, "items": [backup["items"][0]] * 2},
                    {**backup, "items": [None]}]
        missing_last = copy.deepcopy(backup)
        del missing_last["last_item_id"]
        variants.append(missing_last)
        for field, value in (("id", True), ("id", 2**63), ("id", -1),
                             ("url", "javascript:alert(1)"), ("description", "x" * 2001),
                             ("description", "\ud800"), ("is_read", 1), ("starred", None),
                             ("created_at", "bad date"), ("created_at", [])):
            invalid = copy.deepcopy(backup)
            invalid["items"][0][field] = value
            variants.append(invalid)
        incomplete = copy.deepcopy(backup)
        del incomplete["items"][0]["url"]
        variants.append(incomplete)
        for invalid in variants:
            self.assertEqual(self.write("/api/restore", invalid).status_code, 400)
            self.assertEqual(self.client.get("/api/backup").json, backup)
        self.assertEqual(self.client.post("/api/restore", json=backup).status_code, 403)
        self.assertEqual(self.client.post("/api/restore", data="not JSON", content_type="application/json", headers={"X-Later": "1"}).status_code, 400)
        self.assertEqual(self.client.get("/api/backup").json, backup)

    def test_restore_upload_limit_is_separate_from_entry_limit(self):
        self.add()
        backup = self.client.get("/api/backup").json
        source = backup["items"][0]
        backup["items"] = [{**source, "id": n + 1, "description": "x" * 2000} for n in range(20)]
        self.assertGreater(len(json.dumps(backup)), 16 * 1024)
        self.assertEqual(self.write("/api/restore", backup).status_code, 200)
        before = self.client.get("/api/backup").json
        self.assertEqual(self.write("/api/restore", {"padding": "x" * (10 * 1024 * 1024)}).status_code, 413)
        self.assertEqual(self.client.get("/api/backup").json, before)

    def test_restore_database_failure_rolls_back(self):
        self.add()
        self.pop()
        backup = self.client.get("/api/backup").json
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TRIGGER reject_restore BEFORE INSERT ON items BEGIN SELECT RAISE(ABORT, 'test failure'); END")
            connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.write("/api/restore", backup)
        self.assertEqual(self.client.get("/api/backup").json, backup)

    def test_concurrent_unread_pops_are_unique(self):
        for n in range(12):
            self.add(f"https://example.com/{n}")
        def pick(_):
            with self.app.test_client() as client:
                return self.write("/api/pop", {"mode": "unread"}, client=client)
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(pick, range(16)))
        ids = [response.json["item"]["id"] for response in results if response.status_code == 200]
        self.assertEqual(len(ids), 12)
        self.assertEqual(len(set(ids)), 12)
        self.assertEqual(sum(response.status_code == 404 for response in results), 4)


if __name__ == "__main__":
    unittest.main()
