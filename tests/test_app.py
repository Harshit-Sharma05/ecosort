import unittest

from tests.base import AppTestCase, app_module


class SecurityTests(AppTestCase):
    def test_post_without_csrf_token_is_rejected(self):
        r = self.client.post("/login", data={"email": "a@b.co", "password": "x"})
        self.assertEqual(r.status_code, 400)

    def test_secure_cookie_default_is_on(self):
        # The test setup turns it off; the shipped default must be on.
        import os
        path = os.path.join(os.path.dirname(app_module.__file__), "app.py")
        with open(path, encoding="utf-8") as f:
            self.assertIn("SESSION_COOKIE_SECURE=True", f.read())

    def test_safe_next_blocks_other_sites(self):
        with app_module.app.test_request_context("/"):
            safe = app_module.safe_next
            self.assertEqual(safe("/quiz"), "/quiz")
            for bad in ("//evil.com", "https://evil.com", "/\\evil.com",
                        "/ok\r\nSet-Cookie: a=b", "javascript:alert(1)", "", None):
                with self.subTest(bad):
                    self.assertEqual(safe(bad), "/dashboard")


class AccountTests(AppTestCase):
    def test_register_login_logout(self):
        r = self.register()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

        self.client.post("/logout", data={"csrf_token": self.token()})
        self.assertEqual(self.client.get("/dashboard").status_code, 302)  # sent to login

        r = self.login()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_wrong_password_and_duplicate_email(self):
        self.register()
        self.client.post("/logout", data={"csrf_token": self.token()})
        r = self.login(password="wrong-password")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Incorrect email or password", r.data)

        r = self.register()  # same email again
        self.assertIn(b"already exists", r.data)

    def test_register_validation(self):
        r = self.register(password="short")
        self.assertIn(b"at least 8 characters", r.data)
        r = self.register(email="not-an-email")
        self.assertIn(b"valid email", r.data)

    def test_login_redirect_ignores_other_sites(self):
        self.register()
        self.client.post("/logout", data={"csrf_token": self.token()})
        r = self.login(next_url="//evil.com")
        self.assertEqual(r.headers["Location"], "/dashboard")


class ActivityTests(AppTestCase):
    def test_search_and_log_show_on_dashboard(self):
        self.register()
        t = self.token()
        r = self.client.post("/", data={"csrf_token": t, "item": "banana peel"})
        self.assertEqual(r.status_code, 200)

        r = self.client.post("/log", json={"item_name": "Banana peel", "category": "Wet waste",
                                           "material": "Organic waste", "bin": "Wet waste bin"},
                             headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["ok"])
        self.assertIn(b"Banana peel", self.client.get("/dashboard").data)

    def test_log_rejects_bad_input_and_anonymous_users(self):
        t = self.token()
        r = self.client.post("/log", json={"item_name": "x", "category": "Wet waste"},
                             headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 401)

        self.register()
        t = self.token()
        r = self.client.post("/log", json={"item_name": "x", "category": "Not a category"},
                             headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 400)

    def test_clear_history(self):
        self.register()
        t = self.token()
        self.client.post("/log", json={"item_name": "Banana peel", "category": "Wet waste"},
                         headers={"X-CSRF-Token": t})
        self.client.post("/dashboard/clear", data={"csrf_token": t})
        self.assertNotIn(b"Banana peel", self.client.get("/dashboard").data)


class QuizTests(AppTestCase):
    def test_every_question_is_valid(self):
        from quiz_data import BANK
        ids = [q["id"] for q in BANK]
        self.assertEqual(len(ids), len(set(ids)), "duplicate question ids")
        self.assertGreaterEqual(len(BANK), 10)
        for q in BANK:
            with self.subTest(q["id"]):
                self.assertGreaterEqual(len(q["options"]), 2)
                self.assertEqual(len(q["options"]), len(set(q["options"])), "repeated option")
                self.assertTrue(0 <= q["answer"] < len(q["options"]))
                self.assertTrue(q["why"].strip())

    def test_play_and_score(self):
        r = self.client.get("/quiz")
        self.assertEqual(r.status_code, 200)

        from quiz_data import BANK
        chosen = BANK[:3]
        data = {"csrf_token": self.token(), "qids": ",".join(str(q["id"]) for q in chosen)}
        for q in chosen:
            data[f"q{q['id']}"] = str(q["answer"])          # answer all correctly
        r = self.client.post("/quiz", data=data)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"3", r.data)

    def saved_scores(self):
        import db
        conn = db.sqlite3.connect(db.DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM quiz_results").fetchone()[0]
        conn.close()
        return n

    def play_one_question(self):
        from quiz_data import BANK
        q = BANK[0]
        self.client.post("/quiz", data={"csrf_token": self.token(), "qids": str(q["id"]),
                                        f"q{q['id']}": str(q["answer"])})

    def test_quiz_score_saved_only_when_logged_in(self):
        self.play_one_question()                  # not logged in
        self.assertEqual(self.saved_scores(), 0)
        self.register()
        self.play_one_question()                  # logged in
        self.assertEqual(self.saved_scores(), 1)

    def test_garbage_answers_do_not_crash(self):
        r = self.client.post("/quiz", data={"csrf_token": self.token(), "qids": "1,abc,99999,1",
                                            "q1": "zzz"})
        self.assertEqual(r.status_code, 200)


class PageTests(AppTestCase):
    def test_public_pages_load(self):
        for url in ("/", "/quiz", "/login", "/register"):
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_get_on_post_only_routes_redirects(self):
        for url in ("/guided", "/refine"):
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 302)

    def test_search_page_escapes_html(self):
        r = self.client.post("/", data={"csrf_token": self.token(),
                                        "item": "<script>alert(1)</script>"})
        self.assertNotIn(b"<script>alert(1)</script>", r.data)


if __name__ == "__main__":
    unittest.main()
