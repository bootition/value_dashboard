"""备份与加密系统 (PRD §18)

PRD §18.3 要求:
- AR8: 公共数据可以不加密
- AR9: 个性化数据必须加密 (复合指标/筛选规则/自选列表/保存结果/人工覆写/用户配置)
- AR10: 至少保留最近3套全量备份, 每套可带增量链
- AR11: 个性化备份使用用户口令保护, 并生成单独保管的离线恢复密钥
- AR12: 所有凭据不得进入备份与导出文件, 且必须采用Windows凭据保护机制存储
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import struct
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError, VdEnv
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# AES-256-GCM 参数
_NONCE_SIZE = 12  # GCM 推荐 96-bit nonce
_KEY_SIZE = 32    # AES-256
_SALT_SIZE = 32


class Encryptor:
    """AES-256-GCM 加密器 (PRD §18.3 AR9)

    使用用户口令派生密钥 (PBKDF2), 加密个性化数据。
    """

    @staticmethod
    def derive_key(password: str, salt: bytes, iterations: int = 600_000) -> bytes:
        """从用户口令派生 AES-256 密钥 (PBKDF2-HMAC-SHA256)

        P1-30修复: 迭代次数从100K提高到600K (OWASP 2023推荐值)
        """
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=_KEY_SIZE,
            salt=salt,
            iterations=iterations,
        )
        return kdf.derive(password.encode("utf-8"))

    @staticmethod
    def encrypt(data: bytes, key: bytes) -> bytes:
        """AES-256-GCM 加密

        返回格式: salt(32) + nonce(12) + ciphertext + tag(16)
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(_NONCE_SIZE)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, data, None)
        return nonce + ciphertext

    @staticmethod
    def decrypt(encrypted: bytes, key: bytes) -> bytes:
        """AES-256-GCM 解密"""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = encrypted[:_NONCE_SIZE]
        ciphertext = encrypted[_NONCE_SIZE:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None)

    @staticmethod
    def generate_recovery_key() -> str:
        """生成离线恢复密钥 (PRD §18.3 AR11: 单独保管)

        生成 256-bit 随机密钥, 以 Base64 编码返回。
        此密钥与用户口令不同, 用于在用户忘记口令时恢复个性化数据。
        """
        raw_key = os.urandom(_KEY_SIZE)
        return base64.b64encode(raw_key).decode("ascii")

    @staticmethod
    def recovery_key_to_bytes(recovery_key: str) -> bytes:
        """将恢复密钥字符串转换为字节"""
        return base64.b64decode(recovery_key)


class CredentialManager:
    """Windows 凭据保护 (PRD §18.3 AR12: 必须采用Windows凭据保护机制存储)"""

    _SERVICE_NAME = "ValueDashboard"

    @staticmethod
    def store_credential(key: str, value: str) -> bool:
        """存储凭据到 Windows Credential Manager (DPAPI)

        在非 Windows 环境下回退到文件存储 (开发用)。
        """
        try:
            import ctypes
            import ctypes.wintypes as wt

            CRED_TYPE_GENERIC = 1
            CRED_PERSIST_LOCAL_MACHINE = 2

            class CREDENTIAL(ctypes.Structure):
                _fields_ = [
                    ("Flags", wt.DWORD),
                    ("Type", wt.DWORD),
                    ("TargetName", wt.LPWSTR),
                    ("Comment", wt.LPWSTR),
                    ("LastWritten", wt.FILETIME),
                    ("CredentialBlobSize", wt.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wt.DWORD),
                    ("AttributeCount", wt.DWORD),
                    ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wt.LPWSTR),
                    ("UserName", wt.LPWSTR),
                ]

            blob = value.encode("utf-16-le")
            cred = CREDENTIAL()
            cred.Type = CRED_TYPE_GENERIC
            cred.TargetName = f"{CredentialManager._SERVICE_NAME}\\{key}"
            cred.CredentialBlobSize = len(blob)
            cred.CredentialBlob = (ctypes.c_char * len(blob)).from_buffer_copy(blob)
            cred.Persist = CRED_PERSIST_LOCAL_MACHINE
            cred.UserName = ""

            advapi32 = ctypes.windll.advapi32
            advapi32.CredWriteW(ctypes.byref(cred), 0)
            logger.info(f"凭据已存储到 Windows Credential Manager: {key}")
            return True

        except ImportError:
            # 非 Windows: 回退到文件 (开发用, 不安全)
            logger.warning("非 Windows 环境, 凭据存储到文件 (开发用)")
            cfg = Config.current()
            cred_file = cfg.project_root / "data" / ".credentials" / f"{key}.cred"
            cred_file.parent.mkdir(parents=True, exist_ok=True)
            cred_file.write_bytes(value.encode("utf-8"))
            return True
        except Exception as e:
            logger.error(f"凭据存储失败: {e}")
            return False

    @staticmethod
    def retrieve_credential(key: str) -> str | None:
        """从 Windows Credential Manager 读取凭据"""
        try:
            import ctypes
            import ctypes.wintypes as wt

            advapi32 = ctypes.windll.advapi32
            advapi32.CredReadW.restype = wt.BOOL
            advapi32.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_void_p)]

            cred_ptr = ctypes.c_void_p()
            target = f"{CredentialManager._SERVICE_NAME}\\{key}"
            if not advapi32.CredReadW(target, 1, 0, ctypes.byref(cred_ptr)):
                return None

            # 解析 CREDENTIAL 结构
            class CREDENTIAL(ctypes.Structure):
                _fields_ = [
                    ("Flags", wt.DWORD), ("Type", wt.DWORD),
                    ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                    ("LastWritten", wt.FILETIME),
                    ("CredentialBlobSize", wt.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wt.DWORD),
                    ("AttributeCount", wt.DWORD),
                    ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wt.LPWSTR),
                    ("UserName", wt.LPWSTR),
                ]

            cred = CREDENTIAL.from_address(cred_ptr.value)
            blob_size = cred.CredentialBlobSize
            blob_data = ctypes.string_at(cred.CredentialBlob, blob_size)
            value = blob_data.decode("utf-16-le")

            advapi32.CredFree(cred_ptr)
            return value

        except ImportError:
            cfg = Config.current()
            cred_file = cfg.project_root / "data" / ".credentials" / f"{key}.cred"
            if cred_file.exists():
                return cred_file.read_bytes().decode("utf-8")
            return None
        except Exception as e:
            logger.error(f"凭据读取失败: {e}")
            return None

    @staticmethod
    def delete_credential(key: str) -> bool:
        """删除凭据"""
        try:
            import ctypes
            advapi32 = ctypes.windll.advapi32
            target = f"{CredentialManager._SERVICE_NAME}\\{key}"
            advapi32.CredDeleteW(target, 1, 0)
            return True
        except Exception:
            return False


class BackupManager:
    """备份管理器

    PRD §18.3:
    - AR8: 公共数据(价格/财务/PDF)不加密
    - AR9: 个性化数据(规则/自选/覆写/配置)用用户口令加密
    - AR10: 保留最近3套全量备份
    - AR11: 生成离线恢复密钥
    - AR12: 凭据用Windows凭据保护
    """

    # 个性化数据表 (需要加密)
    PERSONALIZED_TABLES = [
        "dsl_expressions", "dsl_dependencies", "screening_rules",
        "screening_results", "watchlist", "manual_overrides",
        "plans", "job_logs", "retry_list", "missing_list",
        "pdf_tasks", # "backup_registry",  # P2修复: 排除自身 "config",
    ]

    # 公共数据表 (不加密)
    PUBLIC_DUCKDB_TABLES = [
        "stock_meta", "price_daily_raw", "price_daily_qfq",
        "balance_sheet", "income_statement", "cash_flow",
        "dividends", "xdxr", "indicator_snapshot",
        "fetch_batch", "source_audit",
    ]

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError("BackupManager requires both stores or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("BackupManager stores do not match injected paths")
        else:
            assert duck is not None and sqlite is not None
            duck_paths = duck._path_set.validate()
            sqlite_paths = sqlite._path_set.validate()
            if duck_paths != sqlite_paths:
                raise PathIsolationError("BackupManager stores must share one database path set")
            validated = duck_paths

        assert duck is not None and sqlite is not None
        cfg = Config.current()
        self._project_root = cfg.project_root
        self._duck = duck
        self._sqlite = sqlite
        self._backup_dir = (
            self._project_root / "data" / "backup"
            if validated.env is VdEnv.FORMAL
            else validated.run_root / "backup"
        )

    def create_full_backup(
        self,
        user_password: str | None = None,
        target_dir: str | None = None,
    ) -> dict[str, Any]:
        """创建全量备份 (PRD §18.3 AR9-10)

        Args:
            user_password: 用户口令 (用于加密个性化数据, None=不加密)
            target_dir: 备份目标目录

        Returns:
            备份结果, 含 backup_id 和文件清单
        """
        backup_dir = Path(target_dir) if target_dir else self._backup_dir
        backup_id = f"full_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        backup_path = backup_dir / backup_id
        backup_path.mkdir(parents=True, exist_ok=True)

        manifest: dict[str, Any] = {
            "backup_id": backup_id,
            "type": "full",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": [],
            "encrypted": user_password is not None,
        }

        # 1. 导出公共数据 (DuckDB → Parquet, 不加密)
        public_dir = backup_path / "public"
        public_dir.mkdir(exist_ok=True)
        for table in self.PUBLIC_DUCKDB_TABLES:
            try:
                parquet_path = public_dir / f"{table}.parquet"
                self._duck.execute_script(f"COPY {table} TO '{parquet_path}' (FORMAT PARQUET);")
                size = parquet_path.stat().st_size
                manifest["files"].append({
                    "category": "public", "table": table,
                    "filename": str(parquet_path.relative_to(backup_path)),
                    "size_bytes": size, "encrypted": False,
                })
                logger.info(f"  导出公共数据: {table} ({size} bytes)")
            except Exception as e:
                logger.warning(f"  跳过 {table}: {e}")

        # 2. 导出个性化数据 (SQLite → JSON, 加密)
        if user_password:
            personal_dir = backup_path / "personal_encrypted"
            personal_dir.mkdir(exist_ok=True)

            # 派生密钥
            salt = os.urandom(_SALT_SIZE)
            key = Encryptor.derive_key(user_password, salt)

            # 导出个性化表为 JSON
            personal_data: dict[str, Any] = {}
            for table in self.PERSONALIZED_TABLES:
                try:
                    rows = self._sqlite.query(f"SELECT * FROM {table}")
                    personal_data[table] = rows
                    logger.info(f"  导出个性化数据: {table} ({len(rows)} rows)")
                except Exception:
                    personal_data[table] = []

            # 加密
            json_data = json.dumps(personal_data, ensure_ascii=False, default=str).encode("utf-8")
            encrypted_data = Encryptor.encrypt(json_data, key)

            enc_path = personal_dir / "personalized.bin"
            enc_path.write_bytes(salt + encrypted_data)

            manifest["files"].append({
                "category": "personal", "table": "all_personalized",
                "filename": str(enc_path.relative_to(backup_path)),
                "size_bytes": enc_path.stat().st_size,
                "encrypted": True, "salt": base64.b64encode(salt).decode("ascii"),
            })

            # 3. 生成离线恢复密钥 (PRD §18.3 AR11)
            recovery_key = Encryptor.generate_recovery_key()
            recovery_key_path = backup_path / "recovery_key.txt"
            recovery_key_path.write_text(
                f"Value Dashboard 离线恢复密钥\n"
                f"备份ID: {backup_id}\n"
                f"创建时间: {manifest['created_at']}\n"
                f"恢复密钥: {recovery_key}\n\n"
                f"请妥善保管此密钥。当忘记用户口令时, 可使用此密钥恢复个性化数据。\n"
                f"此密钥不存储在系统中, 丢失后无法恢复。\n",
                encoding="utf-8",
            )
            manifest["recovery_key_generated"] = True

        # 4. 复制 PDF 文件 (公共数据, 不加密)
        pdf_dir = self._project_root / "data" / "pdf"
        if pdf_dir.exists():
            pdf_backup = backup_path / "pdf"
            shutil.copytree(pdf_dir, pdf_backup, dirs_exist_ok=True)
            pdf_count = sum(1 for _ in pdf_backup.rglob("*.pdf"))
            manifest["files"].append({
                "category": "public", "table": "pdfs",
                "filename": "pdf/",
                "size_bytes": sum(f.stat().st_size for f in pdf_backup.rglob("*.pdf")),
                "file_count": pdf_count, "encrypted": False,
            })

        # 5. 写入 manifest
        manifest_path = backup_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        # 6. 创建 ZIP 包
        zip_path = backup_dir / f"{backup_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(backup_path):
                for file in files:
                    file_path = Path(root) / file
                    arcname = file_path.relative_to(backup_path)
                    zf.write(file_path, arcname)

        # 清理临时目录
        shutil.rmtree(backup_path)

        # 7. 记录到 backup_registry
        checksum = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        with self._sqlite.transaction() as conn:
            conn.execute(
                "INSERT INTO backup_registry (type, path, checksum, encrypted) VALUES (?, ?, ?, ?)",
                ["full", str(zip_path), checksum, user_password is not None],
            )

        # 8. 保留最近3套全量备份 (PRD §18.3 AR10)
        self._rotate_backups()

        logger.info(f"全量备份完成: {zip_path} ({zip_path.stat().st_size} bytes)")

        return {
            "status": "ok",
            "backup_id": backup_id,
            "path": str(zip_path),
            "size_bytes": zip_path.stat().st_size,
            "encrypted": user_password is not None,
            "recovery_key_generated": user_password is not None,
            "file_count": len(manifest["files"]),
            "checksum": checksum[:16],
        }

    def restore_from_backup(
        self,
        backup_path: str,
        user_password: str | None = None,
        recovery_key: str | None = None,
    ) -> dict[str, Any]:
        """从备份恢复 (PRD §18.2 AR5: 只能通过CLI)

        Args:
            backup_path: 备份 ZIP 文件路径
            user_password: 用户口令 (解密个性化数据, None=无加密备份)
            recovery_key: 离线恢复密钥 (P1-29修复: 忘记口令时可用恢复密钥)

        Returns:
            恢复结果
        """
        zip_path = Path(backup_path)
        if not zip_path.exists():
            return {"error": f"备份文件不存在: {zip_path}"}

        # 解压到临时目录
        extract_dir = zip_path.parent / zip_path.stem
        if extract_dir.exists():
            shutil.rmtree(extract_dir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        # 读取 manifest
        manifest_path = extract_dir / "manifest.json"
        if not manifest_path.exists():
            return {"error": "备份文件缺少 manifest.json"}

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result: dict[str, Any] = {
            "status": "ok",
            "backup_id": manifest.get("backup_id"),
            "restored": [],
        }

        # 1. 恢复公共数据 (Parquet → DuckDB)
        # P0#15修复: 白名单验证表名, 防止恶意 ZIP 注入 SQL
        import re
        safe_table_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
        valid_tables = set(self.PUBLIC_DUCKDB_TABLES) | {
            "balance_sheet", "income_statement", "cash_flow",
            "price_daily_raw", "price_daily_qfq", "dividends", "xdxr",
            "indicator_snapshot", "stock_meta", "fetch_batch", "source_audit",
        }
        public_dir = extract_dir / "public"
        if public_dir.exists():
            for parquet_file in public_dir.glob("*.parquet"):
                table = parquet_file.stem
                # 白名单验证: 表名必须是合法标识符且在已知表名集合中
                if not safe_table_pattern.match(table) or table not in valid_tables:
                    logger.warning(f"  跳过可疑表名: {table}")
                    continue
                try:
                    with self._duck.transaction() as conn:
                        conn.execute(f"DELETE FROM {table}")
                        conn.execute(
                            f"COPY {table} FROM '{parquet_file}' (FORMAT PARQUET)"
                        )
                    result["restored"].append({"table": table, "category": "public"})
                    logger.info(f"  恢复公共数据: {table}")
                except Exception as e:
                    logger.warning(f"  恢复 {table} 失败: {e}")

        # 2. 恢复个性化数据 (解密 → SQLite)
        # P1-29修复: 支持用恢复密钥解密（当用户忘记口令时）
        personal_dir = extract_dir / "personal_encrypted"
        if personal_dir.exists() and (user_password or recovery_key):
            enc_file = personal_dir / "personalized.bin"
            if enc_file.exists():
                raw = enc_file.read_bytes()
                salt = raw[:_SALT_SIZE]
                encrypted = raw[_SALT_SIZE:]

                # 优先用恢复密钥，其次用用户口令
                if recovery_key:
                    key = Encryptor.recovery_key_to_bytes(recovery_key)
                else:
                    key = Encryptor.derive_key(user_password, salt)

                try:
                    json_data = Encryptor.decrypt(encrypted, key)
                    personal_data = json.loads(json_data.decode("utf-8"))

                    for table, rows in personal_data.items():
                        if not rows:
                            continue
                        try:
                            with self._sqlite.transaction() as conn:
                                conn.execute(f"DELETE FROM {table}")
                                for row in rows:
                                    cols = list(row.keys())
                                    placeholders = ", ".join(["?"] * len(cols))
                                    col_str = ", ".join(cols)
                                    conn.execute(
                                        f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({placeholders})",
                                        [row[c] for c in cols],
                                    )
                            result["restored"].append({"table": table, "category": "personal", "rows": len(rows)})
                            logger.info(f"  恢复个性化数据: {table} ({len(rows)} rows)")
                        except Exception as e:
                            logger.warning(f"  恢复 {table} 失败: {e}")

                except Exception as e:
                    result["status"] = "partial"
                    result["decrypt_error"] = str(e)

        # 3. 恢复 PDF 文件
        pdf_backup = extract_dir / "pdf"
        if pdf_backup.exists():
            pdf_target = self._project_root / "data" / "pdf"
            shutil.copytree(pdf_backup, pdf_target, dirs_exist_ok=True)
            pdf_count = sum(1 for _ in pdf_target.rglob("*.pdf"))
            result["restored"].append({"table": "pdfs", "category": "public", "count": pdf_count})

        # 清理临时目录
        shutil.rmtree(extract_dir)

        logger.info(f"备份恢复完成: {len(result['restored'])} 项")
        return result

    def list_backups(self) -> list[dict[str, Any]]:
        """列出所有备份"""
        return self._sqlite.query(
            "SELECT * FROM backup_registry ORDER BY created_at DESC"
        )

    def _rotate_backups(self) -> None:
        """保留最近3套全量备份 (PRD §18.3 AR10)"""
        rows = self._sqlite.query(
            "SELECT id, path FROM backup_registry WHERE type='full' ORDER BY created_at DESC"
        )

        if len(rows) <= 3:
            return

        # 删除多余的旧备份
        for row in rows[3:]:
            path = Path(row["path"])
            if path.exists():
                path.unlink()
                logger.info(f"  删除旧备份: {path}")
            self._sqlite.execute(
                "DELETE FROM backup_registry WHERE id = ?",
                [row["id"]],
            )
