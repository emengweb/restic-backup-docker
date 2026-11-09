#!/usr/bin/env python3
"""Read real Buildx provenance from a file, never a process-sized environment value."""
import argparse
import json
from pathlib import Path
import shutil
import sys

MAX_METADATA_BYTES = 64 * 1024 * 1024


def extract(source, output):
    source, output = Path(source), Path(output)
    if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= MAX_METADATA_BYTES:
        raise ValueError("build_metadata_missing_or_oversize")
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("build_metadata_not_object")
    provenance = data.get("buildx.build.provenance")
    if not isinstance(provenance, dict):
        raise ValueError("build_provenance_missing")
    predicate = provenance.get("predicate", provenance)
    if not isinstance(predicate, dict) or not isinstance(predicate.get("buildType"), str) or not predicate["buildType"]:
        raise ValueError("build_provenance_predicate_missing")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, output / "build-metadata.json")
    (output / "provenance.json").write_text(json.dumps(predicate, separators=(",", ":")), encoding="utf-8")
    return {"metadata_bytes": source.stat().st_size, "provenance_preserved": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata_file")
    parser.add_argument("output_directory")
    args = parser.parse_args(argv)
    try:
        result = extract(args.metadata_file, args.output_directory)
    except (ValueError, OSError):
        print("Build metadata/provenance file validation failed", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
