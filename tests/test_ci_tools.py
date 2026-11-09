"""Synthetic immutable-tool extraction; no real Docker daemon is used."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

spec=importlib.util.spec_from_file_location("ci_tools",Path(__file__).resolve().parents[1]/"scripts/install-ci-tool.py")
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class CIToolTests(TestCase):
    def simulate(self, payload, bad=False):
        calls=[]
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1]=="create": return subprocess.CompletedProcess(argv,0,"a"*64,"")
            if argv[1]=="cp": Path(argv[-1]).write_bytes(b"bad" if bad else payload)
            return subprocess.CompletedProcess(argv,0,"","")
        return run,calls
    def test_extracts_verified_binary_without_start_or_run(self):
        payload=b"synthetic-static-binary";run,calls=self.simulate(payload)
        with TemporaryDirectory() as t, mock.patch.dict(module.TOOLS,{("fixture","amd64"):("fixture@sha256:"+"b"*64,"/tool",hashlib.sha256(payload).hexdigest())}):
            dest=Path(t)/"bin/tool";module.install("fixture","amd64",dest,run)
            self.assertEqual(dest.read_bytes(),payload)
            self.assertEqual(dest.stat().st_mode&0o777,0o755)
            self.assertEqual([x[1] for x in calls],["pull","create","cp","rm"])
    def test_checksum_mismatch_still_removes_unstarted_source(self):
        run,calls=self.simulate(b"expected",bad=True)
        with TemporaryDirectory() as t, mock.patch.dict(module.TOOLS,{("fixture","amd64"):("fixture@sha256:"+"b"*64,"/tool",hashlib.sha256(b"expected").hexdigest())}):
            dest=Path(t)/"tool"
            with self.assertRaisesRegex(ValueError,"checksum_mismatch"):module.install("fixture","amd64",dest,run)
            self.assertFalse(dest.exists());self.assertEqual(calls[-1][1],"rm")
    def test_symlink_destination_is_rejected_before_docker(self):
        run,calls=self.simulate(b"x")
        with TemporaryDirectory() as t:
            root=Path(t);(root/"link").symlink_to(root/"target")
            with self.assertRaisesRegex(ValueError,"unsafe_tool_destination"):module.install("syft","amd64",root/"link",run)
            self.assertFalse(calls)
    def test_all_tool_sources_pin_manifest_and_raw_hash(self):
        for image,path,digest in module.TOOLS.values():
            self.assertRegex(image,r"@sha256:[0-9a-f]{64}$")
            self.assertTrue(path.startswith("/"));self.assertRegex(digest,r"^[0-9a-f]{64}$")
