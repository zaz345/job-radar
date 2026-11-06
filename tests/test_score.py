"""Scoring engine unit tests: at least one positive per rule.

Run: python -m tests.test_score
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from core.score import evaluate, load_rules

RULES = load_rules()


def base_job(**over):
    job = {
        "title": "Some generic remote role",
        "description": "We are looking for a person to help with daily tasks. " * 4,
        "skills": [],
        "posted_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "budget_type": None,
        "budget_min": None,
        "budget_max": None,
        "currency": None,
        "proposals": None,
        "required_connects": None,
        "client_spent": None,
        "client_hires": None,
        "client_country": None,
    }
    job.update(over)
    return job


class TestHardFilters(unittest.TestCase):
    def test_pii_veto(self):
        r = evaluate(base_job(description="Need verified emails and LinkedIn profiles list"), RULES)
        self.assertEqual(r["veto"], "red_pii")
        self.assertEqual(r["verdict"], "filtered")

    def test_fulltime_veto(self):
        r = evaluate(base_job(description="This is a full-time role, 40 hrs/week"), RULES)
        self.assertEqual(r["veto"], "red_fulltime")

    def test_negation_does_not_veto(self):
        job = base_job(description="This is not a full-time position; project work only. " * 3)
        r = evaluate(job, RULES)
        self.assertIsNone(r["veto"])

    def test_micro_budget_veto(self):
        r = evaluate(base_job(budget_type="fixed", budget_min=30), RULES)
        self.assertEqual(r["veto"], "red_micro")

    def test_unpaid_trial_veto(self):
        r = evaluate(base_job(description="Complete a test task first, no budget mentioned"), RULES)
        self.assertEqual(r["veto"], "red_trial_unpaid")

    def test_tool_locked_veto(self):
        job = base_job(title="Apollo.io expert for outbound campaigns")
        self.assertEqual(evaluate(job, RULES)["veto"], "red_tool_locked")

    def test_short_tool_keyword_needs_word_boundary(self):
        # 'ghl' must not match 'highly' (real false positive, 2026-11-06)
        job = base_job(title="Ops manager",
                       description="Highly organized, manage many partners and tasks. " * 5)
        self.assertIsNone(evaluate(job, RULES)["veto"])

    def test_pii_recruiter_self_description_not_veto(self):
        # a recruiter describing their own team as having verified LinkedIn profiles is not a harvesting request
        job = base_job(description="All our recruiters have verified LinkedIn profiles. " * 3)
        r = evaluate(job, RULES)
        self.assertNotEqual(r["veto"], "red_pii")

    def test_pii_with_harvest_verb_is_veto(self):
        job = base_job(description="We need you to scrape verified emails from directories at scale")
        self.assertEqual(evaluate(job, RULES)["veto"], "red_pii")

    def test_linkedin_auto_needs_account_context(self):
        # a support-role post mentioning LinkedIn automation tickets is not asking to automate "your account"
        job = base_job(description="Tickets involve deliverability, DNS records, LinkedIn automation. " * 3)
        self.assertNotEqual(evaluate(job, RULES)["veto"], "red_linkedin_auto")


class TestScoring(unittest.TestCase):
    def test_deliverable_and_range_reaches_apply(self):
        job = base_job(
            title="PDF to Excel data cleaning script",
            description="Build a python script to clean invoice data into google sheets. " * 3,
            budget_type="fixed", budget_min=250, proposals=3,
        )
        r = evaluate(job, RULES)
        self.assertIsNone(r["veto"])
        # match_deliverable 30 + budget_fit 20 + low_competition 15 + fresh 15 = 80
        self.assertEqual(r["score"], 80)
        self.assertEqual(r["verdict"], "apply")
        self.assertIn("match_deliverable", r["hits"])

    def test_consider_band(self):
        job = base_job(title="python script for web scraping", budget_type="fixed", budget_min=120)
        r = evaluate(job, RULES)
        self.assertEqual(r["verdict"], "consider")  # 30 + 20 + 15 = 65

    def test_crowded_penalty(self):
        # posted_at set to 72h ago disables the fresh +15, isolating the crowded penalty
        job = base_job(title="python script", proposals=25,
                       posted_at=(datetime.now(timezone.utc) - timedelta(hours=72)).isoformat())
        r = evaluate(job, RULES)
        self.assertIn("-crowded", r["hits"])
        self.assertEqual(r["score"], 10)  # 30 - 20

    def test_score_clamped_at_100(self):
        job = base_job(title="n8n workflow api integration", budget_type="fixed",
                       budget_min=300, proposals=2, client_spent=500, skills=["worldwide"])
        r = evaluate(job, RULES)
        self.assertEqual(r["score"], 100)

    def test_vague_scope_penalty(self):
        job = base_job(title="Help needed", description="please help me")
        r = evaluate(job, RULES)
        self.assertIn("-vague_scope", r["hits"])

    def test_email_snippet_not_penalized_as_vague(self):
        # email-alert bodies are truncated snippets: short does not mean unclear spec, no penalty
        job = base_job(title="Help needed", description="Source 4000 builders contact details",
                       description_source="snippet")
        self.assertNotIn("-vague_scope", evaluate(job, RULES)["hits"])

    def test_job_board_row_without_budget_still_scores(self):
        # typical RemoteOK/Remotive row: no budget, no proposals — must not crash or misfire
        job = base_job(title="Senior Property Manager", description="Manage properties remotely. " * 10,
                       posted_at=(datetime.now(timezone.utc) - timedelta(hours=72)).isoformat())
        r = evaluate(job, RULES)
        self.assertIsNone(r["veto"])
        self.assertEqual(r["score"], 0)


if __name__ == "__main__":
    unittest.main()
