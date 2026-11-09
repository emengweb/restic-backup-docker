#!/usr/bin/env python3
"""Extract verified static CI tools from immutable official images, never start them."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

TOOLS = {
    ("restic", "amd64"): ("restic/restic@sha256:08916bcda4a4435f9d9828ebb4e91bb7ada3d2c8a53699788930e0ae1bd4fa67", "/usr/bin/restic", "20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639"),
    ("restic-legacy", "amd64"): ("restic/restic@sha256:41bd544380380937ed269a89b0b1c6366476a8dcf10ef03c36788eac36cb2450", "/usr/bin/restic", "d852d603d3dd88a73ac2b397c3a18493cf95eddccfc8665692606c32c2a9ff6c"),
    ("syft", "amd64"): ("anchore/syft@sha256:1fe67f9ac8c4c86cbc0075bda33b97a2a804500ee03d450a88721411e133068a", "/syft", "103469021520fc36c94fe5d2139bb45506f520a8043c61b8dcb161053933ba9e"),
    ("syft", "arm64"): ("anchore/syft@sha256:8a2473974a1455306434ddd3c51679ca34ea5667e120dc6d1f56bbee028f4568", "/syft", "16681d296eb9d4c89ffb9556609a8e4b31e370b3b7b4027829338c4190b254e5"),
    ("cosign", "amd64"): ("gcr.io/projectsigstore/cosign@sha256:dfc0619d0ccc361d2632001060e65051cf41ece7280aa68b85877a447e9f935b", "/ko-app/cosign", "be06b2d8da722330fa78fd7cd858a74b589b20ab314a7d901e8cefcf387c965d"),
}


def install(tool, arch, destination, run=subprocess.run):
    image, source, expected = TOOLS[tool, arch]
    destination = Path(destination)
    if not destination.is_absolute() or any(p.is_symlink() for p in [destination, *destination.parents]):
        raise ValueError("unsafe_tool_destination")
    if destination.exists():
        raise ValueError("tool_destination_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    def call(argv):
        r = run(argv, capture_output=True, text=True, timeout=300)
        if r.returncode: raise ValueError("official_tool_image_operation_failed")
        return r.stdout.strip()
    call(["docker", "pull", "--platform", "linux/" + arch, image])
    cid = call(["docker", "create", "--platform", "linux/" + arch, image])
    if len(cid) != 64 or any(x not in "0123456789abcdef" for x in cid):
        raise ValueError("invalid_tool_source_container")
    try:
        with tempfile.TemporaryDirectory(prefix="ci-tool-", dir=destination.parent) as t:
            candidate = Path(t) / "binary"
            call(["docker", "cp", cid + ":" + source, str(candidate)])
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError("invalid_tool_binary")
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
                raise ValueError("official_tool_binary_checksum_mismatch")
            candidate.chmod(0o755)
            os.replace(candidate, destination)
    finally:
        call(["docker", "rm", cid])
    return {"tool": tool, "architecture": arch, "image": image, "raw_sha256": expected, "source_container_started": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("tool", choices=sorted({x[0] for x in TOOLS}))
    p.add_argument("architecture", choices=["amd64", "arm64"])
    p.add_argument("destination")
    a = p.parse_args()
    try: result = install(a.tool, a.architecture, a.destination)
    except (KeyError, ValueError, OSError, subprocess.SubprocessError):
        print("Pinned official CI tool installation failed", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__": sys.exit(main())
