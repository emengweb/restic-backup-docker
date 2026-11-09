"""File-based Buildx metadata regression: real inputs can exceed exec environment limits."""
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

spec = importlib.util.spec_from_file_location("extract_provenance", Path(__file__).resolve().parents[1] / "scripts/extract-build-provenance.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BuildProvenanceTests(TestCase):
    def test_large_metadata_preserved_without_environment_or_argument_payload(self):
        with TemporaryDirectory() as t:
            root = Path(t)
            source = root / "metadata.json"
            predicate = {"buildType": "https://mobyproject.org/buildkit@v1", "materials": [{"uri": "synthetic:fixture", "digest": {"sha256": "a" * 64}}]}
            source.write_text(json.dumps({"buildx.build.provenance": predicate, "buildx.build.metrics": {"synthetic_padding": "x" * 2_000_000}}))
            result = module.extract(source, root / "artifacts")
            self.assertGreater(result["metadata_bytes"], 2_000_000)
            self.assertEqual((root / "artifacts/build-metadata.json").read_bytes(), source.read_bytes())
            self.assertEqual(json.loads((root / "artifacts/provenance.json").read_text()), predicate)

    def test_statement_wrapped_predicate(self):
        with TemporaryDirectory() as t:
            root = Path(t)
            source = root / "metadata.json"
            source.write_text(json.dumps({"buildx.build.provenance": {"predicate": {"buildType": "synthetic-build"}}}))
            module.extract(source, root / "out")
            self.assertEqual(json.loads((root / "out/provenance.json").read_text()), {"buildType": "synthetic-build"})

    def test_missing_real_provenance_is_not_fabricated(self):
        with TemporaryDirectory() as t:
            root = Path(t)
            source = root / "metadata.json"
            source.write_text(json.dumps({"containerimage.digest": "sha256:" + "a" * 64}))
            with self.assertRaises(ValueError): module.extract(source, root / "out")
            self.assertFalse((root / "out").exists())

    def test_metadata_size_and_symlink_guard(self):
        with TemporaryDirectory() as t:
            root = Path(t)
            source = root / "metadata.json"
            source.write_text("{}")
            link = root / "link.json"; link.symlink_to(source)
            with self.assertRaises(ValueError): module.extract(link, root / "out")
            old = module.MAX_METADATA_BYTES
            try:
                module.MAX_METADATA_BYTES = 1
                with self.assertRaises(ValueError): module.extract(source, root / "out")
            finally: module.MAX_METADATA_BYTES = old
