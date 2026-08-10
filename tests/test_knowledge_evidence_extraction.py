import json
import tempfile
import unittest
from pathlib import Path

from app.services.knowledge_evidence_extraction_service import (
    KnowledgeEvidenceExtractionError,
    KnowledgeEvidenceExtractionService,
)


class FakeValidator:
    def __init__(self, html=None, digest="digest-1", final_url="https://learn.microsoft.com/windows/vpn"):
        self.html = html or """<html><head><title>VPN guide</title></head><body><main>
        <h2>Before you begin</h2><p>Before you begin, sign in with an administrator account.</p>
        <h2>Resolve the problem</h2><ol><li>Open Settings and select Network and Internet.</li>
        <li>Confirm that the VPN connection displays Connected.</li></ol>
        <p>If this does not resolve the issue, contact support.</p>
        </main><footer>Advertising and cookie controls</footer></body></html>"""
        self.digest, self.final_url = digest, final_url
        self.fail = None

    def inspect(self, url):
        if self.fail:
            raise self.fail
        return {"http_status": 200, "final_url": self.final_url, "redirect_chain": [],
                "page_title": "VPN guide", "content_type": "text/html",
                "last_modified": "today", "etag": "v1", "content_digest": self.digest,
                "content_preview": self.html}


class KnowledgeEvidenceExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.campaign_root = self.root / "campaigns"
        (self.campaign_root / "research").mkdir(parents=True)
        (self.root / "knowledge_base").mkdir()
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({
            "schema_version": "1.0", "tiers": [{"tier": 1, "label": "First-party",
            "publishers": [{"name": "Microsoft", "domains": ["learn.microsoft.com"]}]}],
            "research_targets": [],
        }), encoding="utf-8")
        self.taxonomy = self.root / "taxonomy.json"
        self.taxonomy.write_text(json.dumps({"schema_version": "1.0", "domains": []}), encoding="utf-8")
        self.research = {
            "schema_version": "1.0", "package_id": "KRP-AAAAAAAAAAAA",
            "campaign_id": "KCP-AAAAAAAAAAAA", "gap_id": "KCG-AAAAAAAAAAAA",
            "work_item_id": "KCW-AAAAAAAAAAAA", "status": "approved",
            "platform": "Windows", "product_vendor": "Microsoft", "created_at": "now",
            "selected_sources": ["KSC-AAAAAAAAAAAA"], "candidate_sources": [{
                "source_candidate_id": "KSC-AAAAAAAAAAAA", "review_state": "selected",
                "topic_relevant": True, "authority_tier": 1, "publisher": "Microsoft",
                "canonical_url": "https://learn.microsoft.com/windows/vpn",
                "page_title": "VPN guide", "applicable_platform": "Windows",
                "provenance": {"content_digest": "research-digest"},
            }],
        }
        self.research_path = self.campaign_root / "research" / "KRP-AAAAAAAAAAAA.json"
        self.research_path.write_text(json.dumps(self.research), encoding="utf-8")
        self.validator = FakeValidator()
        self.service = KnowledgeEvidenceExtractionService(
            self.root, self.campaign_root, self.policy, self.taxonomy, self.validator,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self):
        return self.service.prepare("KRP-AAAAAAAAAAAA", "KSC-AAAAAAAAAAAA")

    def test_requires_approved_selected_authoritative_source(self):
        value = dict(self.research)
        value["status"] = "ready_for_review"
        self.research_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(KnowledgeEvidenceExtractionError, "approved Phase 2"):
            self.prepare()

    def test_prepare_has_stable_kex_identity_and_is_idempotent(self):
        first, second = self.prepare(), self.prepare()
        self.assertRegex(first["extraction_id"], r"^KEX-[A-F0-9]{12}$")
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "proposed")

    def test_extraction_records_retrieval_and_bounded_traceable_units(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        self.assertEqual(package["status"], "needs_review")
        self.assertEqual(package["retrieval"]["requested_url"], self.research["candidate_sources"][0]["canonical_url"])
        self.assertEqual(package["retrieval"]["http_status"], 200)
        self.assertTrue(package["evidence_units"])
        self.assertTrue(all(len(unit["supporting_passage"]) <= self.service.MAX_PASSAGE
                            for unit in package["evidence_units"]))
        self.assertTrue(all(unit["evidence_id"].startswith("EVD-") for unit in package["evidence_units"]))
        self.assertTrue(all(unit["provenance"]["source_candidate_id"] == "KSC-AAAAAAAAAAAA"
                            for unit in package["evidence_units"]))
        self.assertNotIn("Advertising", json.dumps(package["evidence_units"]))

    def test_extraction_never_approves_and_downstream_sees_only_human_approved(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        self.assertTrue(all(unit["review_state"] == "proposed" for unit in package["evidence_units"]))
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]), [])
        target = package["evidence_units"][0]
        reviewed = self.service.review_evidence(package["extraction_id"], target["evidence_id"], "approved", "Checked")
        approved = self.service.approved_units_for(["KRP-AAAAAAAAAAAA"])
        self.assertEqual([unit["evidence_id"] for unit in approved], [target["evidence_id"]])
        self.assertEqual(reviewed["status"], "partially_approved")

    def test_rejected_and_needs_revision_units_are_unavailable(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        first, second = package["evidence_units"][:2]
        self.service.review_evidence(package["extraction_id"], first["evidence_id"], "rejected")
        self.service.review_evidence(package["extraction_id"], second["evidence_id"], "needs_revision")
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]), [])

    def test_unchanged_reextraction_preserves_units_and_history(self):
        first = self.service.extract(self.prepare()["extraction_id"])
        second = self.service.extract(first["extraction_id"])
        self.assertEqual(first["evidence_units"], second["evidence_units"])
        self.assertEqual(first["history"], second["history"])

    def test_source_change_marks_stale_without_destroying_approved_evidence(self):
        package = self.service.extract(self.prepare()["extraction_id"])
        unit = package["evidence_units"][0]
        self.service.review_evidence(package["extraction_id"], unit["evidence_id"], "approved")
        self.validator.digest = "digest-2"
        stale = self.service.refresh_status(package["extraction_id"])
        self.assertEqual(stale["status"], "needs_refresh")
        self.assertTrue(any(value["evidence_id"] == unit["evidence_id"]
                            for value in stale["evidence_units"]))
        self.assertEqual(self.service.approved_units_for(["KRP-AAAAAAAAAAAA"]), [])

    def test_changed_reextraction_preserves_prior_revision(self):
        first = self.service.extract(self.prepare()["extraction_id"])
        self.validator.digest = "digest-2"
        self.validator.html = self.validator.html.replace("Open Settings", "Open Windows Settings")
        second = self.service.extract(first["extraction_id"])
        self.assertEqual(len(second["evidence_revisions"]), 1)
        self.assertEqual(second["evidence_revisions"][0]["source_fingerprint"], "digest-1")
        self.assertTrue(all(unit["review_state"] == "proposed" for unit in second["evidence_units"]))

    def test_unrelated_redirect_and_failed_retrieval_are_governed_failures(self):
        package = self.prepare()
        self.validator.final_url = "https://example.com/unrelated"
        failed = self.service.extract(package["extraction_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("unrelated", failed["retrieval"]["reason"])

    def test_extractor_preserves_modal_language_without_stronger_inference(self):
        self.validator.html = "<html><body><main><p>This change may help resolve the connection problem.</p></main></body></html>"
        package = self.service.extract(self.prepare()["extraction_id"])
        self.assertIn("may help", package["evidence_units"][0]["normalized_claim"])
        self.assertNotIn("will fix", package["evidence_units"][0]["normalized_claim"])


if __name__ == "__main__":
    unittest.main()
