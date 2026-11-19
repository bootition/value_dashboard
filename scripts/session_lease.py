"""跨会话「正式库写入租约」（reports/147）。

为什么需要
----------
多个 AI 会话同时推进时，**没有任何主控**。正式库 `data/valuedashboard.duckdb`
是机器级共享资源：两个人同时跑维护脚本，轻则互相等锁超时，重则一方把另一方
刚写入的行覆盖掉。`AGENTS W4/W8` 只写了「全量 S1 只由主控串行跑」，
但**没有执行器**——规则是概率性的。

本脚本把「谁在什么时候可以写库」变成**文件级租约 + 排队登记**。

设计（参考业界做法）
--------------------
1. **O_EXCL 原子创建**是抢锁的唯一手段 —— POSIX/Windows 上
   `open(path, O_CREAT | O_EXCL)` 是原子的，不需要额外协议
   （参考 `py-filelock` 的 SoftFileLock）。写临时文件再 rename 不是原子抢占。
2. **PID 存活判定优先于年龄**：租约文件里记 `pid` + `hostname`。同机上
   `os.kill(pid, 0)` 不通 → 持有者已死 → 立即回收；跨机才退回年龄判定。
   （`py-filelock` 明确说：age-based expiry 可能与受保护操作重叠，不能单独依赖）
3. **TTL + 续租**：默认 1800s；长任务应每 `TTL/3` 续租一次
   （InferHub 要求 renew interval ≤ TTL/3，避免抖动误判失效）。
4. **排队登记**：没抢到不要忙等。`enqueue` 把你的意图写进队列文件，
   持有者 `status` 时能看到「后面还有谁在等」，`release` 会提示下一个。
5. **过期不等于安全**：回收前仍要求 `pid` 不通或年龄超 TTL 的**两倍宽限**，
   并在日志里写明「回收了谁的锁」。

用法
----
```bash
# 看现在谁在写库 + 排队情况
python scripts/session_lease.py status

# 抢锁（拿不到会打印持有者与剩余时间）
python scripts/session_lease.py acquire --owner fix-data-defects --purpose "清理 44.9 万行 01-01"

# 长任务续租（放在循环里或另开一个会话）
python scripts/session_lease.py renew --owner fix-data-defects

# 释放（会提示队列里的下一个）
python scripts/session_lease.py release --owner fix-data-defects

# 排队登记（没抢到时用，不要忙等）
python scripts/session_lease.py enqueue --owner other-session --purpose "回填 declare_date"
```

租约文件位置：`<仓库 data 目录>/.locks/db-write.lease.json`
—— 放在 `data/` 而不是仓库里，因为它是**机器级共享状态**，且已被 gitignore。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 1800
#: 年龄兜底的宽限倍数：超过 `TTL * 宽限` 才允许在 PID 仍存活时强拆。
STALE_GRACE_FACTOR = 2.0


def _now() -> datetime:
    return datetime.now().astimezone()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _data_dir() -> Path:
    """正式库目录：优先环境变量，其次仓库 data/。"""
    env = os.environ.get("VD_DUCKDB_PATH")
    if env:
        return Path(env).resolve().parent
    return _repo_root() / "data"


def _lock_dir() -> Path:
    directory = _data_dir() / ".locks"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def lease_path() -> Path:
    return _lock_dir() / "db-write.lease.json"


def queue_path() -> Path:
    return _lock_dir() / "db-write.queue.jsonl"


def _pid_alive(pid: int) -> bool:
    """同机上判断 PID 是否存活（跨平台：Windows 用 OpenProcess 语义）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但不属于当前用户 —— 仍然算存活
        return True
    except OSError:
        return False
    return True


def _read_lease() -> dict[str, Any] | None:
    path = lease_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 损坏的租约文件按「已失效」处理，但不能静默删除：
        # 打印出来让人知道曾经有东西写坏了。
        return {"__corrupt__": True, "raw": path.read_text(encoding="utf-8", errors="replace")}


def _lease_state(lease: dict[str, Any]) -> tuple[str, str]:
    """判定租约状态 → (状态, 说明)。状态 ∈ active / dead_owner / expired / corrupt。"""
    if lease.get("__corrupt__"):
        return "corrupt", "租约文件损坏（建议人工确认后删除）"

    owner = lease.get("owner", "?")
    pid = int(lease.get("pid") or 0)
    host = lease.get("hostname")
    expires_at = lease.get("expires_at")
    same_host = host == socket.gethostname()

    # ① 同机优先看 PID：死了就立刻可回收（比年龄准得多）
    if same_host and pid and not _pid_alive(pid):
        return "dead_owner", f"{owner}（pid {pid}）已退出，锁可回收"

    # ② 跨机或 PID 存活：看是否过期
    if expires_at:
        try:
            deadline = datetime.fromisoformat(expires_at)
        except ValueError:
            return "corrupt", f"expires_at 无法解析：{expires_at!r}"
        remaining = (deadline - _now()).total_seconds()
        if remaining > 0:
            return "active", f"{owner}（pid {pid}）持有，剩余 {int(remaining)}s"
        # 过期了：只有在超过宽限期时才允许强拆存活持有者
        overdue = -remaining
        if overdue >= DEFAULT_TTL_SECONDS * (STALE_GRACE_FACTOR - 1):
            return "expired", f"{owner}（pid {pid}）租约已过期 {int(overdue)}s"
        return "active", f"{owner}（pid {pid}）刚过期 {int(overdue)}s，仍给宽限"

    return "expired", f"{owner} 未写 expires_at，按失效处理"


def cmd_status(_args: argparse.Namespace) -> int:
    lease = _read_lease()
    print("═══ 正式库写租约 ═══")
    if lease is None:
        print("状态：🟢 空闲（没有会话持有写租约）")
    else:
        state, detail = _lease_state(lease)
        icon = {"active": "🔴 被占用", "dead_owner": "🟡 持有者已死（可回收）",
                "expired": "🟡 已过期（可回收）", "corrupt": "⚠ 损坏"}[state]
        print(f"状态：{icon} —— {detail}")
        for key in ("owner", "purpose", "acquired_at", "expires_at", "hostname", "command"):
            if lease.get(key):
                print(f"  {key:12} {lease[key]}")

    queue = _read_queue()
    print("\n═══ 排队登记 ═══")
    if not queue:
        print("（空）")
    else:
        for index, item in enumerate(queue, 1):
            print(f"  {index}. {item.get('owner')} —— {item.get('purpose')} "
                  f"（登记于 {item.get('requested_at')}）")

    db = _data_dir() / "valuedashboard.duckdb"
    if db.exists():
        age = (time.time() - db.stat().st_mtime) / 60
        print(f"\n正式库最后写入：{age:.0f} 分钟前")
    return 0


def _read_queue() -> list[dict[str, Any]]:
    path = queue_path()
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def cmd_acquire(args: argparse.Namespace) -> int:
    path = lease_path()
    existing = _read_lease()
    if existing is not None:
        state, detail = _lease_state(existing)
        if state == "active":
            print(f"❌ 拿不到写租约：{detail}")
            print("   不要忙等：用 enqueue 登记排队，或等对方 release 后再抢。")
            return 3
        if state == "corrupt":
            print(f"⚠ 现有租约文件不可用：{detail}")
            print(f"   确认无人写库后手工删除：{path}")
            return 4
        # dead_owner / expired → 允许抢占，但要留下审计记录
        print(f"♻ 回收失效租约：{detail}")
        try:
            path.unlink()
        except OSError as error:
            print(f"❌ 回收失败：{error}")
            return 5

    payload = {
        "owner": args.owner,
        "purpose": args.purpose or "",
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(seconds=args.ttl)).isoformat(),
        "ttl_seconds": args.ttl,
        "command": " ".join(sys.argv),
    }
    try:
        # O_EXCL：原子抢占。哪怕两个会话同时抢，只有一个能成功。
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        print("❌ 抢锁失败：刚被另一个会话抢走（并发冲突）")
        return 3
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(f"✅ 已获得写租约：{args.owner}")
    print(f"   到期时间：{payload['expires_at']}（TTL {args.ttl}s）")
    print(f"   长任务请每 {args.ttl // 3}s 续租一次：session_lease.py renew --owner {args.owner}")
    return 0


def cmd_renew(args: argparse.Namespace) -> int:
    lease = _read_lease()
    if lease is None:
        print("❌ 续租失败：当前没有租约")
        return 3
    if lease.get("owner") != args.owner:
        print(f"❌ 续租失败：租约属于 {lease.get('owner')}，不是 {args.owner}")
        return 3
    ttl = args.ttl or int(lease.get("ttl_seconds") or DEFAULT_TTL_SECONDS)
    lease["expires_at"] = (_now() + timedelta(seconds=ttl)).isoformat()
    lease["renewed_at"] = _now().isoformat()
    lease_path().write_text(json.dumps(lease, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 已续租：{args.owner} 至 {lease['expires_at']}")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    lease = _read_lease()
    if lease is None:
        print("ℹ 当前没有租约（无需释放）")
        return 0
    if lease.get("owner") != args.owner and not args.force:
        print(f"❌ 释放失败：租约属于 {lease.get('owner')}；确认要强拆请加 --force")
        return 3
    lease_path().unlink(missing_ok=True)
    print(f"✅ 已释放写租约（{lease.get('owner')}）")

    queue = _read_queue()
    if queue:
        next_item = queue[0]
        print(f"👉 下一个排队者：{next_item.get('owner')} —— {next_item.get('purpose')}")
        # 出队：把队首移除，剩下的写回
        remaining = queue[1:]
        queue_path().write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in remaining),
            encoding="utf-8",
        )
    return 0


def cmd_enqueue(args: argparse.Namespace) -> int:
    item = {
        "owner": args.owner,
        "purpose": args.purpose or "",
        "requested_at": _now().isoformat(),
        "hostname": socket.gethostname(),
    }
    with queue_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"✅ 已登记排队：{args.owner}")
    print("   轮到你会由持有者 release 时提示；也可以定期 status 查看。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="看谁在写库 + 排队情况")
    status.set_defaults(func=cmd_status)

    for name, help_text, handler in (
        ("acquire", "抢写租约", cmd_acquire),
        ("renew", "续租", cmd_renew),
        ("release", "释放", cmd_release),
        ("enqueue", "排队登记", cmd_enqueue),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--owner", required=True, help="会话标识（如分支名）")
        command.add_argument("--purpose", default="", help="要做什么（会写进租约供他人查看）")
        command.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="租期秒数")
        if name == "release":
            command.add_argument("--force", action="store_true", help="强拆他人租约（慎用）")
        command.set_defaults(func=handler)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
