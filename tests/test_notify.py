"""Notification tests. The one that matters: a job is pushed exactly once, ever.

Run: python -m unittest tests.test_notify
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import db, notify


def _job(**over):
    base = {
        "id": "digest-abc",
        "platform": "upwork",
        "platform_id": "digest-abc",
        "title": "Build an n8n invoice pipeline",
        "company": None,
        "url": "https://www.upwork.com/jobs/~01abc",
        "description": "We need pdf to excel automation.",
        "description_source": "snippet",
        "posted_at": "2026-11-07T05:00:00+00:00",
        "budget_type": "fixed",
        "budget_min": 250.0,
        "budget_max": 250.0,
        "currency": "USD",
        "skills": ["n8n"],
    }
    base.update(over)
    return base


class NotifyCandidatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.init_db(self.conn)
        db.upsert_jobs(self.conn, [_job()])
        self.conn.execute(
            "UPDATE jobs SET score=55, score_hits=?",
            (json.dumps({"hits": ["match_deliverable", "client_quality"], "veto": None}),),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_above_threshold_is_a_candidate(self):
        got = db.notify_candidates(self.conn, 45)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["score"], 55)

    def test_below_threshold_stays_silent(self):
        self.assertEqual(db.notify_candidates(self.conn, 56), [])

    def test_threshold_is_inclusive(self):
        self.assertEqual(len(db.notify_candidates(self.conn, 55)), 1)

    def test_pushed_once_never_twice(self):
        first = db.notify_candidates(self.conn, 45)
        db.mark_notified(self.conn, [r["id"] for r in first])
        self.assertEqual(db.notify_candidates(self.conn, 45), [])
        self.assertIsNotNone(db.last_notified(self.conn))

    def test_shortlisted_job_is_not_pushed(self):
        self.conn.execute("UPDATE jobs SET status='shortlisted'")
        self.conn.commit()
        self.assertEqual(db.notify_candidates(self.conn, 45), [])

    def test_job_board_rows_never_push(self):
        db.upsert_jobs(self.conn, [_job(id="remoteok-1", platform="remoteok", platform_id="1")])
        self.conn.execute("UPDATE jobs SET score=99 WHERE platform='remoteok'")
        self.conn.commit()
        rows = db.notify_candidates(self.conn, 45)
        self.assertEqual([r["platform"] for r in rows], ["upwork"])

    def test_backlog_older_than_window_is_dropped(self):
        # a 30-day-old row must not flood the channel the first time a webhook is set up
        self.conn.execute("UPDATE jobs SET first_seen = '2020-01-01T00:00:00+00:00'")
        self.conn.commit()
        self.assertEqual(db.notify_candidates(self.conn, 45, max_age_days=2), [])

    def test_limit_caps_the_message(self):
        for i in range(5):
            db.upsert_jobs(self.conn, [_job(id=f"digest-{i}", platform_id=f"digest-{i}")])
        self.conn.execute("UPDATE jobs SET score=60")
        self.conn.commit()
        self.assertEqual(len(db.notify_candidates(self.conn, 45, limit=3)), 3)

    def test_scanned_since_counts_new_rows(self):
        n, best = db.scanned_since(self.conn, "2020-01-01T00:00:00+00:00")
        self.assertEqual(n, 1)
        self.assertEqual(best, 55)


class FormatTest(unittest.TestCase):
    ROW = {
        "title": "Build an n8n invoice pipeline",
        "score": 55,
        "score_hits": json.dumps({"hits": ["match_deliverable", "fresh"], "veto": None}),
        "budget_type": "fixed",
        "budget_min": 250.0,
        "budget_max": 250.0,
        "client_country": "United States",
        "url": "https://www.upwork.com/jobs/~01abc",
    }

    def test_message_carries_the_three_things_you_need(self):
        text = notify.format_jobs([self.ROW], 45)
        self.assertIn("Build an n8n invoice pipeline", text)
        self.assertIn("55分", text)
        self.assertIn("https://www.upwork.com/jobs/~01abc", text)

    def test_rule_ids_are_translated_not_leaked(self):
        text = notify.format_jobs([self.ROW], 45)
        self.assertIn("技能对口", text)
        self.assertNotIn("match_deliverable", text)

    def test_hourly_budget_reads_correctly(self):
        row = dict(self.ROW, budget_type="hourly", budget_min=5.0, budget_max=40.0)
        self.assertIn("$5–40/h", notify.format_jobs([row], 45))

    def test_unknown_rule_id_is_shown_raw_not_dropped(self):
        row = dict(self.ROW, score_hits=json.dumps({"hits": ["brand_new_rule"]}))
        self.assertIn("brand_new_rule", notify.format_jobs([row], 45))

    def test_long_title_is_truncated_with_ellipsis(self):
        row = dict(self.ROW, title="x" * 120)
        text = notify.format_jobs([row], 45)
        self.assertIn("…", text)
        self.assertNotIn("x" * 60, text)

    def test_heartbeat_is_one_line_of_fact(self):
        text = notify.format_heartbeat(12, 40, 45, 1)
        self.assertIn("12", text)
        self.assertIn("40", text)


class SendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.orig = notify.LOG_PATH
        notify.LOG_PATH = Path(self.tmp.name) / "notify.log"

    def tearDown(self):
        notify.LOG_PATH = self.orig
        self.tmp.cleanup()

    def test_no_webhook_does_not_raise_and_still_logs(self):
        ok, detail = notify.send("hello", None)
        self.assertFalse(ok)
        self.assertEqual(detail, "no webhook")
        self.assertTrue(notify.LOG_PATH.exists())
        self.assertIn("hello", notify.LOG_PATH.read_text(encoding="utf-8"))

    def test_unreachable_webhook_returns_false_instead_of_raising(self):
        ok, detail = notify.send("hello", "http://127.0.0.1:9/nonexistent")
        self.assertFalse(ok)
        self.assertIn("FAILED", notify.LOG_PATH.read_text(encoding="utf-8"))


class DeliverTest(unittest.TestCase):
    """Channel routing: webhook first, email as the no-WeCom fallback, log otherwise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.orig = notify.LOG_PATH
        notify.LOG_PATH = Path(self.tmp.name) / "notify.log"

    def tearDown(self):
        notify.LOG_PATH = self.orig
        self.tmp.cleanup()

    def test_no_channel_is_silent_and_logged(self):
        ok, detail = notify.deliver("subj", "body", {"webhook": None, "smtp": None})
        self.assertFalse(ok)
        self.assertEqual(detail, "no channel configured")
        self.assertIn("body", notify.LOG_PATH.read_text(encoding="utf-8"))

    def test_unreachable_smtp_returns_false_instead_of_raising(self):
        smtp = {"host": "127.0.0.1", "port": 9, "user": "a@b.c", "pass": "x", "to": "d@e.f"}
        ok, detail = notify.deliver("subj", "body", {"webhook": None, "smtp": smtp})
        self.assertFalse(ok)
        self.assertIn("FAILED email", notify.LOG_PATH.read_text(encoding="utf-8"))

    def test_email_used_when_webhook_absent(self):
        smtp = {"host": "127.0.0.1", "port": 9, "user": "a@b.c", "pass": "x", "to": "d@e.f"}
        ok, detail = notify.deliver("subj", "body", {"webhook": None, "smtp": smtp})
        # delivery fails (nothing listening), but the email path is the one that was tried
        self.assertNotIn("no channel configured", detail)


class RenderTest(unittest.TestCase):
    ROW = {
        "title": "Build an n8n invoice pipeline",
        "score": 55,
        "score_hits": json.dumps({"hits": ["match_deliverable"]}),
        "budget_type": "fixed",
        "budget_min": 250.0,
        "budget_max": 250.0,
        "client_country": "United States",
        "url": "https://www.upwork.com/jobs/~01abc",
    }

    def test_links_become_tappable_anchors(self):
        html = notify._to_html(notify.format_jobs([self.ROW], 45))
        self.assertIn('<a href="https://www.upwork.com/jobs/~01abc"', html)

    def test_bold_becomes_tags(self):
        html = notify._to_html("**hello**")
        self.assertIn("<b>hello</b>", html)

    def test_wecom_font_tag_does_not_leak_into_html(self):
        html = notify._to_html(notify.format_jobs([self.ROW], 45))
        self.assertNotIn("<font color", html)

    def test_subject_single_job_names_it(self):
        s = notify.subject_for([self.ROW])
        self.assertIn("55", s)
        self.assertIn("n8n", s)

    def test_subject_multiple_jobs_counts_them(self):
        s = notify.subject_for([self.ROW, dict(self.ROW, score=60)])
        self.assertIn("2", s)
        self.assertIn("60", s)


class SmtpSettingsTest(unittest.TestCase):
    def test_none_when_credentials_missing(self):
        from core.config import smtp_settings

        self.assertIsNone(smtp_settings({"UPWORK_IMAP_USER": "", "UPWORK_IMAP_PASS": ""}))

    def test_reuses_imap_password_and_self_addresses(self):
        from core.config import smtp_settings

        cfg = smtp_settings({"UPWORK_IMAP_USER": "me@163.com", "UPWORK_IMAP_PASS": "abc"})
        self.assertEqual(cfg["user"], "me@163.com")
        self.assertEqual(cfg["pass"], "abc")
        self.assertEqual(cfg["to"], "me@163.com")  # empty NOTIFY_EMAIL_TO -> send to self
        self.assertEqual(cfg["host"], "smtp.163.com")

    def test_explicit_recipient_wins(self):
        from core.config import smtp_settings

        cfg = smtp_settings({
            "UPWORK_IMAP_USER": "me@163.com",
            "UPWORK_IMAP_PASS": "abc",
            "NOTIFY_EMAIL_TO": "phone@qq.com",
        })
        self.assertEqual(cfg["to"], "phone@qq.com")


if __name__ == "__main__":
    unittest.main()
