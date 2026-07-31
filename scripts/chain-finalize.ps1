# scripts/chain-finalize.ps1 — 串行执行正式库收尾链（后台运行）
# 顺序: 等待分红 → 隔离 → 财务重建(最长) → 快照重算 → 诊断
param(
    [int]$WaitPid = 237348
)

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
$env:VD_ENV = "formal"
$env:VD_FORMAL_ACK = "confirmed"
$env:VD_DUCKDB_PATH = "D:\Mr.Q\掌控经济\value-dashboard\data\valuedashboard.duckdb"
$env:VD_SQLITE_PATH = "D:\Mr.Q\掌控经济\value-dashboard\data\valuedashboard.sqlite"
$log = "D:\Mr.Q\掌控经济\value-dashboard\scripts\evidence\chain_finalize.log"
Set-Location "D:\Mr.Q\掌控经济\value-dashboard"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $log -Value "[$ts] $msg"
}

# 1) 等待分红进程结束
Log "等待分红进程 PID=$WaitPid ..."
while (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) {
    Start-Sleep -Seconds 20
}
Log "分红进程已结束"

# 2) 隔离遗留 lineage
Log "== 步骤2: 隔离遗留 lineage =="
python -c "
import sys, json
sys.path.insert(0, r'D:\Mr.Q\掌控经济\value-dashboard')
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import resolve_and_validate_paths
from app.core.data_maintenance import legacy_quarantine_summary, quarantine_legacy_records
p = resolve_and_validate_paths()
duck = DuckDBStore(paths=p)
print('before:', json.dumps(legacy_quarantine_summary(duck), ensure_ascii=False))
print('quarantined:', json.dumps(quarantine_legacy_records(duck), ensure_ascii=False))
"
if ($LASTEXITCODE -ne 0) { Log "隔离失败 exit=$LASTEXITCODE"; exit 1 }
Log "隔离完成"

# 3) 财务 lineage 重建（全市场，最长）
Log "== 步骤3: 财务 lineage 重建（后台内串行执行）=="
python scripts/repair_financials.py --resume --rate-limit 0.3 2>&1 | Out-File -Append -FilePath $log
Log "财务重建完成 exit=$LASTEXITCODE"

# 4) 快照重算
Log "== 步骤4: 快照重算 =="
python -c "
import sys, json
sys.path.insert(0, r'D:\Mr.Q\掌控经济\value-dashboard')
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.storage.path_policy import resolve_and_validate_paths
from app.core.indicators.calculator import IndicatorCalculator
p = resolve_and_validate_paths()
duck = DuckDBStore(paths=p); sqlite = SQLiteStore(paths=p)
r = IndicatorCalculator(duck=duck, sqlite=sqlite).compute_snapshot_for_all()
print(json.dumps(r, ensure_ascii=False, default=str))
"
Log "快照重算完成 exit=$LASTEXITCODE"

# 5) 诊断
Log "== 步骤5: 诊断 =="
python -c "
import sys, json
sys.path.insert(0, r'D:\Mr.Q\掌控经济\value-dashboard')
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.storage.path_policy import resolve_and_validate_paths
from app.core.data_quality import build_data_quality_status, screening_readiness
from datetime import datetime, timezone
p = resolve_and_validate_paths()
duck = DuckDBStore(paths=p); sqlite = SQLiteStore(paths=p)
r = {
  'captured_at': datetime.now(timezone.utc).isoformat(),
  'quality': build_data_quality_status(duck, sqlite),
  'screening_ready': screening_readiness(duck, sqlite),
}
out = r'D:\Mr.Q\掌控经济\value-dashboard\docs\evidence-final-diagnostics.json'
open(out, 'w', encoding='utf-8').write(json.dumps(r, ensure_ascii=False, indent=2, default=str))
print('diagnostics written to', out)
print('ready=', r['screening_ready']['ready'], 'warnings=', r['screening_ready']['warning_codes'])
"
Log "诊断完成 exit=$LASTEXITCODE"
Log "== 链式收尾全部完成 =="
