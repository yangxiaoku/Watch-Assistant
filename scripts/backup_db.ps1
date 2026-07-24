param(
    [string]$Container = "watch-assistant"
)

$ErrorActionPreference = "Stop"
$script = @'
import datetime
import pathlib
import sqlite3

source = pathlib.Path("/data/watch-assistant.db")
backup_dir = pathlib.Path("/data/backups")
backup_dir.mkdir(parents=True, exist_ok=True)
target = backup_dir / f"watch-assistant-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
    source_db.backup(target_db)
backups = sorted(backup_dir.glob("watch-assistant-*.db"), reverse=True)
for expired in backups[7:]:
    expired.unlink()
print(target)
'@

docker exec $Container python -c $script
