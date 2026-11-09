"""Immutable image capabilities, independent of runtime Pod or node identity."""
from pathlib import Path
import json
from .errors import BackupError

PROFILE_PATH = Path("/usr/share/k3s-restic/profile.json")


def capabilities():
    if not PROFILE_PATH.exists():
        # A source checkout is not a deployed image. Unit/native developer tooling
        # still uses explicit native executable checks; production images carry metadata.
        return None
    try: data = json.loads(PROFILE_PATH.read_text())
    except (OSError, ValueError): raise BackupError("invalid_image_profile", "Image capability metadata is invalid") from None
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise BackupError("invalid_image_profile", "Image capability metadata is not supported")
    if not isinstance(data.get("postgres_majors"), list) or not isinstance(data.get("mariadb_series"), list):
        raise BackupError("invalid_image_profile", "Image database capabilities are missing")
    return data


def check_engine(engine, target_major=None):
    p = capabilities()
    if p is None or engine == "files": return
    key = "postgres_majors" if engine == "postgres" else "mariadb_series"
    available = [str(x) for x in p.get(key, [])]
    if not available:
        raise BackupError("image_profile_mismatch", "This image label does not contain the requested database engine")
    if target_major is not None and str(target_major) not in available:
        raise BackupError("image_profile_mismatch", "This image label does not contain the requested restore-server version")


def pg_probe_major(config):
    if config.target_major:
        check_engine("postgres", config.target_major)
        return str(config.target_major)
    p = capabilities()
    if p is None: return "17"
    available = [str(x) for x in p.get("postgres_majors", [])]
    if not available: raise BackupError("image_profile_mismatch", "No PostgreSQL tools are available in this image label")
    return max(available, key=int)
