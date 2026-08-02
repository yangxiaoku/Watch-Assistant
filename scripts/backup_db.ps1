param(
    [string]$Container = "watch-assistant"
)

$ErrorActionPreference = "Stop"
$script = @'
import datetime
import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile

source = pathlib.Path("/data/watch-assistant.db")
backup_dir = pathlib.Path("/data/backups")
backup_dir.mkdir(parents=True, exist_ok=True)
release = os.environ.get("WATCH_ASSISTANT_RELEASE", "")
if len(release) != 40 or any(char not in "0123456789abcdefABCDEF" for char in release):
    raise SystemExit("explicit full WATCH_ASSISTANT_RELEASE is required")
release = release.lower()
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
backup_id = f"watch-assistant-{stamp}-{os.urandom(4).hex()}"
target = backup_dir / f"{backup_id}.db"
manifest = backup_dir / f"{backup_id}.json"
temporary = backup_dir / f".{backup_id}.db.tmp"
try:
    with sqlite3.connect(source) as source_db, sqlite3.connect(temporary) as target_db:
        source_db.backup(target_db)
    with sqlite3.connect(temporary) as check_db:
        if check_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    size_bytes = temporary.stat().st_size
    os.replace(temporary, target)
    payload = {
        "schema_version": 1,
        "backup_id": backup_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "database_file": target.name,
        "size_bytes": size_bytes,
        "sha256": digest,
        "release": release,
        "integrity_check": "ok",
        "restore": "preview_or_explicit_manual_command_only",
    }
    descriptor, manifest_tmp_name = tempfile.mkstemp(
        prefix=f".{manifest.name}.", suffix=".tmp", dir=backup_dir
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(manifest_tmp_name, manifest)
    finally:
        pathlib.Path(manifest_tmp_name).unlink(missing_ok=True)
    records = sorted(backup_dir.glob("watch-assistant-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for expired_manifest in records[7:]:
        try:
            expired = json.loads(expired_manifest.read_text(encoding="utf-8"))
            expired_file = expired.get("database_file")
            if isinstance(expired_file, str) and pathlib.Path(expired_file).name == expired_file:
                (backup_dir / expired_file).unlink(missing_ok=True)
            expired_manifest.unlink(missing_ok=True)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
except Exception:
    temporary.unlink(missing_ok=True)
    target.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    raise
print(target)
'@

docker exec $Container python -c $script
