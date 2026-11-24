"""「朴素本地时间」棘轮：不许再有新的 `datetime.now()`（无时区）混进时间口径。

## 为什么需要这条
2026-11-24 我把 `job_logs` 里的 **UTC** 时间当成本地时间，把一轮 **58 秒**的正常更新
误判成"卡了 8 小时"，还据此提交了一个基于错误前提的修复。

同一天又发现 `manual_overrides` **同一张表内两种基准**：
* `created_at` 走 `CURRENT_TIMESTAMP`（SQLite 给 UTC）
* `rolled_back_at` 却由 CLI 写 `datetime.now().isoformat()`（朴素本地）

只要有人把这两列并排展示，同一次误判就会重演。

## 这条棘轮怎么工作
扫 `app/` 下所有 `.py`，找出 `datetime.now()` / `datetime.today()` 这类**无时区**调用，
与显式允许清单比对：
* 清单里的（显示用的日志时间、备份文件名）—— 与"库里存的时间"无关，允许；
* 清单外的 —— **红**，强制新代码显式选 `datetime.now(UTC)` 或来清单里登记理由。
"""

from __future__ import annotations

import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent.parent / "app"

#: 允许保留"朴素本地时间"的位置：**只用于给人看的显示/文件名，绝不写进数据库时间列**。
ALLOWED_NAIVE_NOW: dict[str, str] = {
    "core/auto_update.py": "进度日志的 HH:MM:SS 标签（给人看的本地时钟，不入库）",
    "core/backup/manager.py": "备份 id 的文件名时间戳（不入库、不参与比较）",
}

#: 无时区的时间调用。三类**不会**被命中：
#:   * `now(tz)` / `now(UTC)` —— 带参数，本就 aware；
#:   * `now().astimezone()` —— 产出 aware 本地时间（显式表态，不是"忘了时区"）；
#:   * `utcnow()` 已废弃，单独点名抓它。
_NAIVE_PATTERN = re.compile(
    r"datetime\.(now|today)\(\s*\)(?!\.astimezone)|datetime\.utcnow\(\s*\)"
)


def _code_part(line: str) -> str:
    """去掉行尾注释再匹配 —— 否则**注释里提到** `datetime.now()` 也会被误报。"""
    return line.split("#", 1)[0]


def _scan() -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for path in APP_ROOT.rglob("*.py"):
        rel = path.relative_to(APP_ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _NAIVE_PATTERN.search(_code_part(line)):
                hits.setdefault(rel, []).append(f"{rel}:{lineno}: {line.strip()[:100]}")
    return hits


def test_no_unregistered_naive_local_time() -> None:
    hits = _scan()
    offenders = {rel: lines for rel, lines in hits.items() if rel not in ALLOWED_NAIVE_NOW}
    assert offenders == {}, (
        "发现未登记的「朴素本地时间」调用。数据库时间列一律用 `datetime.now(UTC)`；"
        "确实只用于显示/文件名的，请登记到 ALLOWED_NAIVE_NOW 并写明理由。\n"
        + "\n".join(line for lines in offenders.values() for line in lines)
    )


def test_allowlist_entries_still_exist() -> None:
    """清单里的条目如果已经不存在了，说明代码变了 —— 该把清单也删掉（防清单腐化）。"""
    hits = _scan()
    stale = [rel for rel in ALLOWED_NAIVE_NOW if rel not in hits]
    assert stale == [], f"这些允许项已经不再出现，请从 ALLOWED_NAIVE_NOW 删除：{stale}"


def test_manual_overrides_rollback_uses_utc() -> None:
    """`manual_overrides.rolled_back_at` 必须与同表的 `created_at`（UTC）同基准。"""
    source = (APP_ROOT / "cli" / "main.py").read_text(encoding="utf-8")
    assert "UPDATE manual_overrides SET rolled_back_at" in source
    # 找到那条 UPDATE 附近的写入参数，必须带 UTC
    idx = source.index("UPDATE manual_overrides SET rolled_back_at")
    window = source[idx : idx + 400]
    assert "datetime.now(UTC).isoformat()" in window, (
        "rolled_back_at 必须写 UTC（同表 created_at 是 CURRENT_TIMESTAMP=UTC）"
    )
