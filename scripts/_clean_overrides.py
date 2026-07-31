import sys; from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(PROJECT_ROOT))
from app.core.storage.path_policy import require_formal_maintenance_paths
from app.core.storage.sqlite_store import SQLiteStore
s = SQLiteStore(paths=require_formal_maintenance_paths())
before = s.query("SELECT COUNT(*) AS cnt FROM manual_overrides WHERE status != 'published' AND rolled_back_at IS NULL")[0]['cnt']
s.execute("DELETE FROM manual_overrides WHERE status != 'published' AND rolled_back_at IS NULL")
after = s.query("SELECT COUNT(*) AS cnt FROM manual_overrides WHERE status != 'published' AND rolled_back_at IS NULL")[0]['cnt']
print(f"before={before}, after={after}")
