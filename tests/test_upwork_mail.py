"""Upwork alert email parsing tests.

Run: python -m tests.test_upwork_mail

Samples follow the real 2026-11-06 email plain-text layout (same field order and separators),
but IDs and company names are fictional.
"""
from __future__ import annotations

import unittest

from adapters.upwork_mail import parse_body, parse_email

POSTED = "2026-11-06T13:45:57+00:00"


def body(jobs: list[str]) -> str:
    head = (
        "\n\nNew job alert\n\nHi Anzi,\n\n"
        "This job was just posted and matches your alert settings.\n\n----------\n\n"
    )
    return head + "\n\n".join(jobs) + (
        "\n\n    ----------\n\nView job details: https://www.upwork.com/jobs/~0999?frkscc=abc\n\n"
        "You received this email because Freelancer Plus members have access to job alerts.\n"
    )


HOURLY = """AI Software Engineer – Long-Term Remote Collabo...: https://www.upwork.com/jobs/~022118700635253528249?link=title&utm_medium=email

  Hourly: $25.00 - $47.00 · Est. time: More than 6 months · Posted Fri, Nov 06

We’re looking for an experienced engineer to help build AI-powered applications. We want someone who can understand... more: https://www.upwork.com/jobs/~022118700635253528249?link=more

Skills:
Artificial Intelligence: https://www.upwork.com/s/emails/nx/search/jobs/?a=1
React: https://www.upwork.com/s/emails/nx/search/jobs/?a=2
+3 more

Payment verified · 4.98 stars · $78K spent · United States"""

FIXED = """Build a PDF to Excel converter...: https://www.upwork.com/jobs/~011122334455667788

  Fixed-price: $250.00 · Posted Fri, Nov 06

Need a script that turns scanned invoices into a spreadsheet. Deliverables: csv and xlsx.... more: https://www.upwork.com/jobs/~011122334455667788?link=more

Skills:
Python: https://www.upwork.com/s/emails/nx/search/jobs/?a=3
Data Extraction: https://www.upwork.com/s/emails/nx/search/jobs/?a=4

Payment verified · $1.2M spent · Canada"""

NO_CLIENT = """Simple web scraping task...: https://www.upwork.com/jobs/~055566677788899900

  Fixed-price: $80.00 · Posted Fri, Nov 06

Scrape product prices from three sites into Google Sheets."""


class TestParseBody(unittest.TestCase):
    def test_hourly_job_all_fields(self):
        jobs = parse_body(body([HOURLY]), posted_at=POSTED, subject="New job alert: x")
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual(j["platform_id"], "~022118700635253528249")
        self.assertEqual(j["budget_type"], "hourly")
        self.assertEqual(j["budget_min"], 25.0)
        self.assertEqual(j["budget_max"], 47.0)
        self.assertEqual(j["client_country"], "United States")
        self.assertEqual(j["client_rating"], 4.98)
        self.assertEqual(j["client_spent"], 78000.0)
        self.assertEqual(j["payment_verified"], 1)
        self.assertEqual(j["skills"], ["Artificial Intelligence", "React"])
        self.assertEqual(j["description_source"], "snippet")
        self.assertIn("experienced engineer", j["description"])
        self.assertNotIn("more:", j["description"])
        self.assertEqual(j["title"], "AI Software Engineer – Long-Term Remote Collabo")

    def test_fixed_price_and_million_spent(self):
        jobs = parse_body(body([FIXED]), posted_at=POSTED, subject="New job alert: y")
        j = jobs[0]
        self.assertEqual(j["budget_type"], "fixed")
        self.assertEqual(j["budget_min"], 250.0)
        self.assertEqual(j["budget_max"], 250.0)
        self.assertEqual(j["client_spent"], 1_200_000.0)
        self.assertEqual(j["client_country"], "Canada")

    def test_client_block_absent_leaves_fields_none(self):
        j = parse_body(body([NO_CLIENT]), posted_at=POSTED, subject="z")[0]
        self.assertIsNone(j["payment_verified"])
        self.assertIsNone(j["client_spent"])
        self.assertIsNone(j["client_country"])
        self.assertEqual(j["budget_min"], 80.0)

    def test_link_lines_do_not_create_phantom_jobs(self):
        # the trailing more: and the bottom View job details both point to the same job — not two entries
        jobs = parse_body(body([HOURLY]), posted_at=POSTED, subject="z")
        self.assertEqual(len(jobs), 1)

    def test_digest_with_two_jobs(self):
        jobs = parse_body(body([HOURLY, FIXED]), posted_at=POSTED, subject="New jobs that match")
        self.assertEqual([j["platform_id"] for j in jobs],
                         ["~022118700635253528249", "~011122334455667788"])


class TestParseEmail(unittest.TestCase):
    def _mime(self, text: str, subject: str) -> bytes:
        return (
            "From: Upwork Notification <donotreply@upwork.com>\r\n"
            f"Subject: {subject}\r\n"
            "Date: Fri, 06 Nov 2026 13:45:57 +0000\r\n"
            "Message-ID: <abc@upwork.com>\r\n"
            'Content-Type: text/plain; charset="utf-8"\r\n'
            "MIME-Version: 1.0\r\n\r\n"
            + text
        ).encode("utf-8")

    def test_mime_roundtrip_gives_id_and_utc_time(self):
        jobs = parse_email(self._mime(body([HOURLY]), "New job alert: AI"))
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual(j["platform"], "upwork")
        self.assertEqual(j["posted_at"], POSTED)
        self.assertEqual(j["id"], jobs[0]["id"])
        self.assertEqual(len(j["id"]), 16)
        self.assertEqual(j["raw"]["message_id"], "<abc@upwork.com>")

    def test_same_job_twice_gives_same_id(self):
        a = parse_email(self._mime(body([HOURLY]), "s"))[0]["id"]
        b = parse_email(self._mime(body([HOURLY]), "s"))[0]["id"]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
