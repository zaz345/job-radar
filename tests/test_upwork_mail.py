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


# --- daily digest: HTML only, no job id anywhere in the message -------------------------

DIGEST_HTML = """<html><body>
<table><tr><td>Your job alert for web scraping.</td></tr></table>
<table>
  <tr><td>Daily job digest</td></tr>
  <tr><td>Posted 6 hours ago</td></tr>
  <tr><td>List of Canadian Retirement Home emails</td></tr>
  <tr><td>Fixed-price &bull; $30</td></tr>
  <tr><td>Looking for a detail-oriented freelancer to extract contact email addresses from a public directory.</td></tr>
  <tr><td>Key Responsibilities:</td></tr>
  <tr><td>Access th... <a href="https://link.t.upwork.com/ls/click?upn=xxx">more</a></td></tr>
  <tr><td>Data Entry</td></tr>
  <tr><td>Data Scraping</td></tr>
  <tr><td>List Building</td></tr>
  <tr><td>Payment verified</td></tr>
  <tr><td>&bull; 5.0</td></tr>
  <tr><td>&bull; $3k spent</td></tr>
  <tr><td>&bull; Canada</td></tr>
  <tr><td>Posted 1 hour ago</td></tr>
  <tr><td>Web Scraper, Need Product Name</td></tr>
  <tr><td>Fixed-price &bull; $50</td></tr>
  <tr><td>Pretty simple. We need images and SKUs from our vendor site...</td></tr>
  <tr><td>Python</td></tr>
  <tr><td>Adobe Photoshop</td></tr>
  <tr><td>+2</td></tr>
  <tr><td>Payment verified</td></tr>
  <tr><td>&bull; 4.2</td></tr>
  <tr><td>&bull; $34k spent</td></tr>
  <tr><td>&bull; United States</td></tr>
  <tr><td>View all alerts</td></tr>
  <tr><td>530 Lytton Ave, Suite 301, Palo Alto, CA 94301</td></tr>
  <tr><td>&copy; 2015 - 2026 Upwork Global LLC</td></tr>
</table>
</body></html>"""


def digest_mime(html: str, subject: str = "New jobs that match your saved search") -> bytes:
    import base64

    payload = base64.b64encode(html.encode("utf-8")).decode("ascii")
    return (
        "From: Upwork Notification <donotreply@upwork.com>\r\n"
        f"Subject: {subject}\r\n"
        "Date: Sat, 07 Nov 2026 01:01:11 +0000\r\n"
        "Message-ID: <digest@upwork.com>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/html; charset="utf-8"\r\n'
        "Content-Transfer-Encoding: base64\r\n\r\n"
        + payload
    ).encode("utf-8")


class TestParseDigest(unittest.TestCase):
    def setUp(self):
        self.jobs = parse_email(digest_mime(DIGEST_HTML))

    def test_two_jobs_found(self):
        self.assertEqual(len(self.jobs), 2)
        self.assertEqual(self.jobs[0]["title"], "List of Canadian Retirement Home emails")
        self.assertEqual(self.jobs[1]["title"], "Web Scraper, Need Product Name")

    def test_budget_and_skills(self):
        j = self.jobs[0]
        self.assertEqual(j["budget_type"], "fixed")
        self.assertEqual(j["budget_min"], 30.0)
        self.assertEqual(j["skills"], ["Data Entry", "Data Scraping", "List Building"])
        self.assertIn("detail-oriented freelancer", j["description"])
        self.assertNotIn("more", j["description"].lower())

    def test_client_block_bullet_form(self):
        j = self.jobs[0]
        self.assertEqual(j["payment_verified"], 1)
        self.assertEqual(j["client_rating"], 5.0)
        self.assertEqual(j["client_spent"], 3000.0)
        self.assertEqual(j["client_country"], "Canada")

    def test_footer_does_not_leak_into_country(self):
        # the last posting runs straight into the mail footer
        j = self.jobs[1]
        self.assertEqual(j["client_country"], "United States")
        self.assertNotIn("Lytton", j["client_country"] or "")

    def test_posted_at_derived_from_mail_date(self):
        # mail sent 01:01 UTC, first job "Posted 6 hours ago" -> 19:01 previous day
        self.assertEqual(self.jobs[0]["posted_at"], "2026-11-06T19:01:11+00:00")
        self.assertEqual(self.jobs[1]["posted_at"], "2026-11-07T00:01:11+00:00")

    def test_no_job_id_so_fingerprint_is_stable(self):
        j = self.jobs[0]
        self.assertTrue(j["platform_id"].startswith("digest-"))
        again = parse_email(digest_mime(DIGEST_HTML))[0]
        self.assertEqual(j["id"], again["id"])
        self.assertIn("nx/search/jobs", j["url"])

    def test_plus_n_marker_is_not_a_skill(self):
        self.assertNotIn("+2", self.jobs[1]["skills"])
        self.assertEqual(self.jobs[1]["skills"], ["Python", "Adobe Photoshop"])


if __name__ == "__main__":
    unittest.main()
