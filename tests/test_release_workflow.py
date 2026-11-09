"""Regression guards for observed release-only workflow failures (no YAML dependency)."""
from pathlib import Path
import re
from unittest import TestCase


class ReleaseWorkflowTests(TestCase):
    def test_checkout_precedes_evidence_downloads_for_release_jobs(self):
        text = (Path(__file__).resolve().parents[1]/".github/workflows/docker-publish.yml").read_text()
        jobs = dict(re.findall(r"^  ([a-z][a-z-]+):\n(.*?)(?=^  [a-z][a-z-]+:|\Z)", text, re.M|re.S))
        for name in ("publish-architecture", "publish-index"):
            job = jobs[name]
            checkout = job.index("uses: actions/checkout@")
            download = job.index("uses: actions/download-artifact@")
            self.assertLess(checkout, download, name+": checkout clean deletes downloaded evidence")
            self.assertEqual(job.count("uses: actions/checkout@"), 1)
            self.assertIn("github.ref_type == 'tag'", job)
            self.assertIn("ENABLE_GHCR_PUBLISH", job)
            self.assertIn("scripts/install-ci-tool.py cosign", job)

    def test_published_image_evidence_and_signing_are_not_optional(self):
        text = (Path(__file__).resolve().parents[1]/".github/workflows/docker-publish.yml").read_text()
        self.assertIn("sha256sum --check --strict SHA256SUMS", text)
        self.assertIn('cosign sign --yes "$IMAGE@$PUBLISHED_DIGEST"', text)
        self.assertIn('cosign sign --yes "$IMAGE@$digest"', text)
        self.assertNotIn("continue-on-error: true", text)
        self.assertNotIn("steps.build.outputs.metadata", text)
