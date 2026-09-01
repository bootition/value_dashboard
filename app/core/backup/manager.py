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
import hmac
import json
import logging
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.maintenance import exclusive_maintenance
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError, VdEnv
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# AES-256-GCM 参数
_NONCE_SIZE = 12  # GCM 推荐 96-bit nonce
_KEY_SIZE = 32    # AES-256
_SALT_SIZE = 32
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_ARCHIVE_MEMBER_SIZE = 1_024 * 1_024 * 1_024
_MAX_ARCHIVE_UNCOMPRESSED_SIZE = 10 * 1_024 * 1_024 * 1_024
_MAX_ARCHIVE_COMPRESSION_RATIO = 100


def recover_pending_restore(paths: DatabasePathSet) -> None:
    """Recover a durable restore rollback only when a journal actually exists."""
    validated = paths.validate()
    data_root = (
        Config.current().project_root / "data"
        if validated.env is VdEnv.FORMAL
        else validated.run_root / "data"
    )
    if not (data_root / ".restore-journal.json").exists():
        return
    manager = BackupManager(
        duck=DuckDBStore(paths=validated), sqlite=SQLiteStore(paths=validated), paths=validated
    )
    manager.recover_interrupted_restore()


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
         "pdf_tasks", "pdf_archive_manifest",
         # "backup_registry",  # P2修复: 排除自身 "config",
    ]
    _PERSONALIZED_DELETE_ORDER = [
        "watchlist", "screening_results", "dsl_dependencies", "screening_rules",
        "dsl_expressions", "manual_overrides", "plans", "job_logs", "retry_list",
        "missing_list", "pdf_tasks", "pdf_archive_manifest",
    ]
    _PERSONALIZED_INSERT_ORDER = [
        "dsl_expressions", "dsl_dependencies", "screening_rules", "screening_results",
        "watchlist", "manual_overrides", "plans", "job_logs", "retry_list",
        "missing_list", "pdf_tasks", "pdf_archive_manifest",
    ]

    # 公共数据表 (不加密)
    PUBLIC_DUCKDB_TABLES = [
        "stock_meta", "price_daily_raw", "price_daily_qfq",
        "balance_sheet", "income_statement", "cash_flow",
        "dividends", "xdxr", "indicator_snapshot",
        "fetch_batch", "source_audit", "raw_response_archive",
        "raw_response_archive_history",
        "source_audit_quarantine", "dividends_quarantine",
    ]
    LEGACY_PUBLIC_DUCKDB_TABLES = [
        "stock_meta", "price_daily_raw", "price_daily_qfq",
        "balance_sheet", "income_statement", "cash_flow",
        "dividends", "xdxr", "indicator_snapshot",
        "fetch_batch", "source_audit", "raw_response_archive_history",
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
        self._data_root = (
            self._project_root / "data"
            if validated.env is VdEnv.FORMAL
            else validated.run_root / "data"
        )
        self._backup_dir = self._data_root / "backup"
        self._restore_journal_path = self._data_root / ".restore-journal.json"

    def create_full_backup(
        self,
        user_password: str | None = None,
        target_dir: str | None = None,
    ) -> dict[str, Any]:
        """Create one cross-store snapshot while file writers are excluded."""
        with exclusive_maintenance(self._duck.db_path):
            return self._create_full_backup(user_password, target_dir)

    def _create_full_backup(
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
        backup_dir = (Path(target_dir) if target_dir else self._backup_dir).resolve()
        backup_id = f"full_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"
        backup_path = backup_dir / backup_id
        backup_path.mkdir(parents=True, exist_ok=True)

        if not user_password:
            return {
                "status": "error",
                "error": "a password is required to protect personalized data",
            }

        manifest: dict[str, Any] = {
            "backup_id": backup_id,
            "type": "full",
            "created_at": datetime.now(UTC).isoformat(),
            "files": [],
            "encrypted": user_password is not None,
        }

        # 1. Export one complete supported schema generation under one write
        # lock. An older complete database can be backed up before its first
        # schema migration; a partial table set is never a full backup.
        public_dir = backup_path / "public"
        public_dir.mkdir(exist_ok=True)
        try:
            with self._duck.write_connection() as connection:
                public_tables = self._detect_public_tables(connection)
            for table in public_tables:
                with self._duck.write_connection() as connection:
                    if table in self._BLOB_CHUNK_TABLES:
                        parts = self._export_public_table_chunked(connection, table, public_dir)
                        if parts:
                            for part in parts:
                                manifest["files"].append({
                                    "category": "public", "table": table,
                                    "filename": part["filename"],
                                    "size_bytes": part["size_bytes"],
                                    "sha256": part["sha256"],
                                    "encrypted": False,
                                    "part": part["index"],
                                })
                        else:
                            parquet_path = public_dir / f"{table}.parquet"
                            target = str(parquet_path).replace("'", "''")
                            connection.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
                            manifest["files"].append({
                                "category": "public", "table": table,
                                "filename": parquet_path.relative_to(backup_path).as_posix(),
                                "size_bytes": parquet_path.stat().st_size,
                                "sha256": _file_checksum(parquet_path),
                                "encrypted": False,
                            })
                        logger.info(f"  导出公共数据: {table} parts={len(parts)}")
                    else:
                        parquet_path = public_dir / f"{table}.parquet"
                        target = str(parquet_path).replace("'", "''")
                        connection.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
                        size = parquet_path.stat().st_size
                        manifest["files"].append({
                            "category": "public", "table": table,
                            "filename": parquet_path.relative_to(backup_path).as_posix(),
                            "size_bytes": size,
                            "sha256": _file_checksum(parquet_path),
                            "encrypted": False,
                        })
                        logger.info(f"  导出公共数据: {table} ({size} bytes)")
        except Exception as error:
            shutil.rmtree(backup_path, ignore_errors=True)
            return {"status": "error", "error": f"public backup failed: {error}"}

        # 2. Export personalized data once, then encrypt the data key for both
        # the password and the separately delivered recovery key.
        personal_dir = backup_path / "personal_encrypted"
        personal_dir.mkdir(exist_ok=True)
        data_key = os.urandom(_KEY_SIZE)
        password_salt = os.urandom(_SALT_SIZE)
        password_key = Encryptor.derive_key(user_password, password_salt)
        recovery_key = Encryptor.generate_recovery_key()
        recovery_key_bytes = Encryptor.recovery_key_to_bytes(recovery_key)

        personal_data: dict[str, Any] = {}
        for table in self.PERSONALIZED_TABLES:
            rows = self._sqlite.query(f"SELECT * FROM {table}")
            personal_data[table] = rows
            logger.info(f"  导出个性化数据: {table} ({len(rows)} rows)")

        encrypted_data = Encryptor.encrypt(
            json.dumps(personal_data, ensure_ascii=False, default=str).encode("utf-8"),
            data_key,
        )
        enc_path = personal_dir / "personalized.bin"
        enc_path.write_bytes(encrypted_data)
        key_path = personal_dir / "keys.json"
        key_path.write_text(
            json.dumps(
                {
                    "password_salt": base64.b64encode(password_salt).decode("ascii"),
                    "password_wrapped_key": base64.b64encode(
                        Encryptor.encrypt(data_key, password_key)
                    ).decode("ascii"),
                    "recovery_wrapped_key": base64.b64encode(
                        Encryptor.encrypt(data_key, recovery_key_bytes)
                    ).decode("ascii"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest["files"].append({
            "category": "personal", "table": "all_personalized",
            "filename": enc_path.relative_to(backup_path).as_posix(),
            **_file_manifest(enc_path, backup_path),
            "encrypted": True,
        })
        manifest["files"].append({
            "category": "personal", "table": "key_envelopes",
            "filename": key_path.relative_to(backup_path).as_posix(),
            **_file_manifest(key_path, backup_path),
            "encrypted": True,
        })
        manifest["recovery_key_generated"] = True

        # 4. 复制 PDF 文件 (公共数据, 不加密)
        pdf_dir = self._data_root / "pdf"
        if pdf_dir.exists():
            pdf_backup = backup_path / "pdf"
            shutil.copytree(pdf_dir, pdf_backup, dirs_exist_ok=True)
            pdf_count = sum(1 for _ in pdf_backup.rglob("*.pdf"))
            manifest["files"].append({
                "category": "public", "table": "pdfs",
                "filename": "pdf",
                **_directory_manifest(pdf_backup, backup_path),
                "file_count": pdf_count, "encrypted": False,
            })

        cold_pdf_dir = self._cold_pdf_dir()
        if cold_pdf_dir.exists():
            cold_backup = backup_path / "pdf_archive"
            shutil.copytree(cold_pdf_dir, cold_backup)
            manifest["files"].append({
                "category": "public", "table": "archive_pdfs",
                "filename": "pdf_archive",
                **_directory_manifest(cold_backup, backup_path),
                "encrypted": False,
            })

        # 5. 写入 manifest
        manifest_path = backup_path / "manifest.json"
        manifest["authentication"] = _manifest_authentication(manifest, data_key)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        # 6. 创建 ZIP 包
        zip_path = backup_dir / f"{backup_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(backup_path):
                for file in files:
                    file_path = Path(root) / file
                    arcname = file_path.relative_to(backup_path)
                    zf.write(file_path, arcname)

        # 清理临时目录
        shutil.rmtree(backup_path)

        # 7. 记录到 backup_registry
        checksum = _file_checksum(zip_path)
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
            "recovery_key": recovery_key,
            "file_count": len(manifest["files"]),
            "checksum": checksum[:16],
        }

    _BLOB_CHUNK_TABLES = {"raw_response_archive_history"}
    _BLOB_CHUNK_SIZE = 5000

    def _detect_public_tables(self, connection: Any) -> list[str]:
        present_tables = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        for tables in (self.PUBLIC_DUCKDB_TABLES, self.LEGACY_PUBLIC_DUCKDB_TABLES):
            if present_tables.issuperset(tables):
                return tables
        missing = sorted(set(self.LEGACY_PUBLIC_DUCKDB_TABLES) - present_tables)
        raise RuntimeError(
            "database schema is incomplete; missing required public tables: "
            + ", ".join(missing)
        )

    def _cold_pdf_dir(self) -> Path:
        from app.core.pdf.manager import PDFManager

        return PDFManager(sqlite=self._sqlite).archive_root

    def _unwrap_backup_data_key(
        self,
        personal_dir: Path,
        user_password: str | None,
        recovery_key: str | None,
    ) -> bytes:
        keys = json.loads((personal_dir / "keys.json").read_text(encoding="utf-8"))
        if recovery_key:
            wrapping_key = Encryptor.recovery_key_to_bytes(recovery_key)
            wrapped_key = base64.b64decode(keys["recovery_wrapped_key"])
        elif user_password:
            salt = base64.b64decode(keys["password_salt"])
            wrapping_key = Encryptor.derive_key(user_password, salt)
            wrapped_key = base64.b64decode(keys["password_wrapped_key"])
        else:
            raise ValueError("password or recovery key is required")
        return Encryptor.decrypt(wrapped_key, wrapping_key)

    def _validate_backup_manifest(
        self,
        extract_dir: Path,
        manifest: dict[str, Any],
        user_password: str | None,
        recovery_key: str | None,
    ) -> None:
        if manifest.get("type") != "full" or not isinstance(manifest.get("files"), list):
            raise ValueError("invalid backup manifest")
        data_key = self._unwrap_backup_data_key(extract_dir / "personal_encrypted", user_password, recovery_key)
        authentication = manifest.get("authentication")
        if not isinstance(authentication, str) or not hmac.compare_digest(
            authentication, _manifest_authentication(manifest, data_key)
        ):
            raise ValueError("backup manifest authentication failed")
        for entry in manifest["files"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str):
                raise ValueError("invalid backup file entry")
            filename = entry["filename"].replace("\\", "/")
            path = (extract_dir / filename).resolve()
            if not path.is_relative_to(extract_dir.resolve()):
                raise ValueError("backup manifest path escapes extraction root")
            if path.is_file():
                if entry.get("size_bytes") != path.stat().st_size or entry.get("sha256") != _file_checksum(path):
                    raise ValueError(f"backup file checksum mismatch: {filename}")
            elif path.is_dir():
                actual = _directory_manifest(path, extract_dir)
                if (
                    entry.get("size_bytes") != actual["size_bytes"]
                    or entry.get("sha256") != actual["sha256"]
                    or entry.get("files") != actual["files"]
                ):
                    raise ValueError(f"backup directory checksum mismatch: {filename}")
            else:
                raise ValueError(f"backup file is missing: {filename}")
        expected_files = {"manifest.json"}
        for entry in manifest["files"]:
            filename = entry["filename"].replace("\\", "/")
            if "files" in entry:
                expected_files.update(
                    f"{filename.rstrip('/')}/{item['filename']}" for item in entry["files"]
                )
            else:
                expected_files.add(filename)
        actual_files = {
            path.relative_to(extract_dir).as_posix()
            for path in extract_dir.rglob("*")
            if path.is_file()
        }
        if actual_files != expected_files:
            raise ValueError("backup archive contains unlisted files")

    def restore_from_backup(
        self,
        backup_path: str,
        user_password: str | None = None,
        recovery_key: str | None = None,
    ) -> dict[str, Any]:
        """Restore only while this profile rejects writes from every process."""
        with exclusive_maintenance(
            self._duck.db_path,
            reclaim_abandoned=self._restore_journal_path.exists(),
        ):
            self._recover_interrupted_restore_locked()
            return self._restore_from_backup(backup_path, user_password, recovery_key)

    def recover_interrupted_restore(self) -> None:
        """Rollback a restore that was interrupted after its first store commit."""
        with exclusive_maintenance(self._duck.db_path):
            self._recover_interrupted_restore_locked()

    def _recover_interrupted_restore_locked(self) -> None:
        if not self._restore_journal_path.exists():
            return
        try:
            journal = json.loads(self._restore_journal_path.read_text(encoding="utf-8"))
            rollback_root = Path(journal["rollback_root"])
            if not rollback_root.is_dir() or not rollback_root.is_relative_to(self._data_root.resolve()):
                raise ValueError("restore rollback journal is unsafe")
            public_files = []
            for parquet_file in rollback_root.glob("*.parquet"):
                table = parquet_file.name[:-len(".parquet")]
                if ".part" in table:
                    table = table.split(".part")[0]
                if table in self.PUBLIC_DUCKDB_TABLES:
                    public_files.append((table, parquet_file))
            if not public_files:
                raise ValueError("restore rollback data is incomplete")
            personal_path = rollback_root / "personalized.json"
            personal_data = json.loads(personal_path.read_text(encoding="utf-8"))
            if not isinstance(personal_data, dict):
                raise ValueError("restore rollback personalized data is invalid")
            self._restore_public_tables(public_files)
            self._restore_personalized_data(personal_data)
            self._restore_pdf_tree(rollback_root / "pdf", self._data_root / "pdf")
            self._restore_pdf_tree(rollback_root / "pdf_archive", self._cold_pdf_dir())
        except Exception as error:
            raise RuntimeError(
                "interrupted restore recovery failed; preserve the profile and repair "
                f"{self._restore_journal_path}: {error}"
            ) from error
        shutil.rmtree(rollback_root, ignore_errors=True)
        self._restore_journal_path.unlink(missing_ok=True)

    @staticmethod
    def _restore_pdf_tree(source: Path, target: Path) -> None:
        if target.exists():
            shutil.rmtree(target)
        if not source.exists():
            return
        shutil.copytree(source, target)

    def _restore_from_backup(
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
        zip_path = Path(backup_path).resolve()
        if not zip_path.exists():
            return {"status": "error", "error": f"备份文件不存在: {zip_path}"}

        registered = self._sqlite.query(
            "SELECT checksum FROM backup_registry WHERE path = ? ORDER BY id DESC LIMIT 1",
            [str(zip_path)],
        )
        if registered and _file_checksum(zip_path) != registered[0]["checksum"]:
            return {"status": "error", "error": "backup checksum mismatch"}

        # Own a fresh extraction directory; never delete a sibling derived from
        # an attacker-controlled archive stem.
        extract_dir = Path(tempfile.mkdtemp(prefix="vd-restore-", dir=zip_path.parent))

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                members = zf.infolist()
                if len(members) > _MAX_ARCHIVE_MEMBERS:
                    return {"status": "error", "error": "backup archive has too many members"}
                total_uncompressed = 0
                for member in members:
                    total_uncompressed += member.file_size
                    if member.file_size > _MAX_ARCHIVE_MEMBER_SIZE:
                        return {"status": "error", "error": "backup archive member is too large"}
                    if total_uncompressed > _MAX_ARCHIVE_UNCOMPRESSED_SIZE:
                        return {"status": "error", "error": "backup archive is too large"}
                    if member.compress_size and member.file_size / member.compress_size > _MAX_ARCHIVE_COMPRESSION_RATIO:
                        return {"status": "error", "error": "backup archive compression ratio is unsafe"}
                    target = (extract_dir / member.filename).resolve()
                    if not target.is_relative_to(extract_dir.resolve()):
                        return {"status": "error", "error": "unsafe backup archive path"}
                zf.extractall(extract_dir)
        except (OSError, zipfile.BadZipFile) as error:
            return {"status": "error", "error": f"invalid backup archive: {error}"}

        # 读取 manifest
        manifest_path = extract_dir / "manifest.json"
        if not manifest_path.exists():
            return {"status": "error", "error": "备份文件缺少 manifest.json"}

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self._validate_backup_manifest(extract_dir, manifest, user_password, recovery_key)
        except (OSError, ValueError, KeyError, TypeError) as error:
            shutil.rmtree(extract_dir, ignore_errors=True)
            return {"status": "error", "error": f"backup manifest validation failed: {error}"}
        result: dict[str, Any] = {
            "status": "ok",
            "backup_id": manifest.get("backup_id"),
            "restored": [],
        }

        # 1. Validate and decrypt every restore input before mutating either database.
        # P0#15修复: 白名单验证表名, 防止恶意 ZIP 注入 SQL
        import re
        safe_table_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
        valid_tables = set(self.PUBLIC_DUCKDB_TABLES)
        public_files: list[tuple[str, Path]] = []
        public_dir = extract_dir / "public"
        if public_dir.exists():
            for parquet_file in public_dir.rglob("*.parquet"):
                table = parquet_file.name[:-len(".parquet")]
                if ".part" in table:
                    table = table.split(".part")[0]
                if not safe_table_pattern.match(table) or table not in valid_tables:
                    return {"status": "error", "error": f"unsafe public table: {table}"}
                public_files.append((table, parquet_file))
        expected_public_tables = {
            item["table"]
            for item in manifest.get("files", [])
            if item.get("category") == "public" and item.get("table") not in {"pdfs", "archive_pdfs"}
        }
        found_public_tables = {table for table, _ in public_files}
        accepted_public_table_sets = {
            frozenset(self.PUBLIC_DUCKDB_TABLES),
            frozenset(self.LEGACY_PUBLIC_DUCKDB_TABLES),
        }
        if (
            expected_public_tables not in accepted_public_table_sets
            or expected_public_tables != found_public_tables
        ):
            shutil.rmtree(extract_dir, ignore_errors=True)
            return {"status": "error", "error": "backup public tables do not match manifest"}
        personal_data: dict[str, list[dict[str, Any]]] | None = None
        personal_dir = extract_dir / "personal_encrypted"
        if personal_dir.exists():
            if not (user_password or recovery_key):
                shutil.rmtree(extract_dir, ignore_errors=True)
                return {"status": "error", "error": "password or recovery key is required"}
            try:
                encrypted = (personal_dir / "personalized.bin").read_bytes()
                data_key = self._unwrap_backup_data_key(personal_dir, user_password, recovery_key)
                parsed = json.loads(Encryptor.decrypt(encrypted, data_key).decode("utf-8"))
                if not isinstance(parsed, dict) or not set(parsed).issubset(self.PERSONALIZED_TABLES):
                    raise ValueError("unsafe personalized table")
                personal_data = {table: list(rows) for table, rows in parsed.items()}
            except Exception as error:
                shutil.rmtree(extract_dir, ignore_errors=True)
                return {"status": "error", "error": f"personal restore failed: {error}"}

        rollback_dir = self._data_root / f".restore-rollback-{uuid.uuid4().hex}"
        rollback_dir.mkdir(parents=True, exist_ok=True)
        pdf_backup = extract_dir / "pdf"
        pdf_target = self._data_root / "pdf"
        rollback_pdf_dir = rollback_dir / "pdf"
        staged_pdf_dir = extract_dir / "staged-pdf"
        archive_pdf_backup = extract_dir / "pdf_archive"
        archive_pdf_target = self._cold_pdf_dir()
        rollback_archive_pdf_dir = rollback_dir / "pdf_archive"
        staged_archive_pdf_dir = extract_dir / "staged-pdf-archive"
        try:
            self._export_public_tables(rollback_dir)
            previous_personal_data = self._snapshot_personalized_data()
            (rollback_dir / "personalized.json").write_text(
                json.dumps(previous_personal_data, ensure_ascii=False, default=str), encoding="utf-8"
            )
            if pdf_target.exists():
                shutil.copytree(pdf_target, rollback_pdf_dir)
            if pdf_backup.exists():
                shutil.copytree(pdf_backup, staged_pdf_dir)
            if archive_pdf_target.exists():
                shutil.copytree(archive_pdf_target, rollback_archive_pdf_dir)
            if archive_pdf_backup.exists():
                shutil.copytree(archive_pdf_backup, staged_archive_pdf_dir)
            self._restore_journal_path.write_text(
                json.dumps({"rollback_root": str(rollback_dir.resolve())}), encoding="utf-8"
            )
        except Exception as error:
            shutil.rmtree(extract_dir, ignore_errors=True)
            return {"status": "error", "error": f"restore snapshot failed: {error}"}

        try:
            self._restore_public_tables(public_files)
            result["restored"].extend(
                {"table": table, "category": "public"} for table, _ in public_files
            )
        except Exception as error:
            shutil.rmtree(extract_dir, ignore_errors=True)
            return {"status": "error", "error": f"public restore failed: {error}"}

        # 2. Restore personalized state. If it fails, restore the public database
        # from its pre-restore snapshot before returning an error.
        if personal_data is not None:
            try:
                self._restore_personalized_data(personal_data)
                result["restored"].append(
                    {"table": "all_personalized", "category": "personal"}
                )
            except Exception as error:
                try:
                    self._restore_public_tables(
                        [(table, rollback_dir / f"{table}.parquet") for table in self.PUBLIC_DUCKDB_TABLES]
                    )
                    self._restore_personalized_data(previous_personal_data)
                except Exception as rollback_error:
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    return {
                        "status": "error",
                        "error": f"restore failed and rollback failed: {rollback_error}",
                    }
                shutil.rmtree(rollback_dir, ignore_errors=True)
                self._restore_journal_path.unlink(missing_ok=True)
                shutil.rmtree(extract_dir, ignore_errors=True)
                return {"status": "error", "error": f"personal restore failed: {error}"}

        # 3. Restore PDF trees from fully staged copies. On failure, compensate both
        # databases and the previous PDF tree before reporting an error.
        try:
            # A backup without a PDF tree represents an empty tree, not "leave
            # whatever happens to be live". _restore_pdf_tree handles both.
            self._restore_pdf_tree(staged_pdf_dir, pdf_target)
            self._restore_pdf_tree(staged_archive_pdf_dir, archive_pdf_target)
            result["restored"].append({
                "table": "pdfs", "category": "public",
                "count": sum(1 for _ in pdf_target.rglob("*.pdf")) if pdf_target.exists() else 0,
            })
            result["restored"].append({
                "table": "archive_pdfs", "category": "public",
                "count": sum(1 for _ in archive_pdf_target.rglob("*.pdf")) if archive_pdf_target.exists() else 0,
            })
        except Exception as error:
            try:
                self._restore_public_tables(
                    [(table, rollback_dir / f"{table}.parquet") for table in self.PUBLIC_DUCKDB_TABLES]
                )
                self._restore_personalized_data(previous_personal_data)
                self._restore_pdf_tree(rollback_pdf_dir, pdf_target)
                self._restore_pdf_tree(rollback_archive_pdf_dir, archive_pdf_target)
            except Exception as rollback_error:
                shutil.rmtree(extract_dir, ignore_errors=True)
                return {
                    "status": "error",
                    "error": f"pdf restore failed and rollback failed: {rollback_error}",
                }
            shutil.rmtree(rollback_dir, ignore_errors=True)
            self._restore_journal_path.unlink(missing_ok=True)
            shutil.rmtree(extract_dir, ignore_errors=True)
            return {"status": "error", "error": f"pdf restore failed: {error}"}

        # 清理临时目录
        shutil.rmtree(extract_dir)
        shutil.rmtree(rollback_dir, ignore_errors=True)
        self._restore_journal_path.unlink(missing_ok=True)

        logger.info(f"备份恢复完成: {len(result['restored'])} 项")
        return result

    @staticmethod
    def _export_public_table_chunked(connection: Any, table: str, public_dir: Path) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        last_hash = ""
        index = 0
        while True:
            rows = connection.execute(
                f"SELECT COUNT(*) FROM (SELECT * FROM {table} "
                f"WHERE raw_response_hash > ? ORDER BY raw_response_hash LIMIT ?)",
                [last_hash, BackupManager._BLOB_CHUNK_SIZE],
            ).fetchone()[0]
            if rows == 0:
                break
            out = public_dir / f"{table}.part{index:04d}.parquet"
            target = str(out).replace("'", "''")
            connection.execute(
                f"COPY (SELECT * FROM {table} WHERE raw_response_hash > ? "
                f"ORDER BY raw_response_hash LIMIT ?) TO '{target}' (FORMAT PARQUET)",
                [last_hash, BackupManager._BLOB_CHUNK_SIZE],
            )
            chunk_rows = connection.execute(
                f"SELECT raw_response_hash FROM {table} "
                f"WHERE raw_response_hash > ? ORDER BY raw_response_hash "
                f"LIMIT {BackupManager._BLOB_CHUNK_SIZE}",
                [last_hash],
            ).fetchall()
            last_hash = chunk_rows[-1][0]
            parts.append({
                "index": index,
                "filename": out.name,
                "size_bytes": out.stat().st_size,
                "sha256": _file_checksum(out),
            })
            index += 1
        return parts

    def _export_public_tables(self, target_dir: Path) -> None:
        with self._duck.write_connection() as connection:
            for table in self.PUBLIC_DUCKDB_TABLES:
                if table in self._BLOB_CHUNK_TABLES:
                    parts = self._export_public_table_chunked(connection, table, target_dir)
                    if not parts:
                        target = str(target_dir / f"{table}.parquet").replace("'", "''")
                        connection.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
                else:
                    target = str(target_dir / f"{table}.parquet").replace("'", "''")
                    connection.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")

    def _restore_public_tables(self, files: list[tuple[str, Path]]) -> None:
        by_table: dict[str, list[Path]] = {}
        for table, path in files:
            by_table.setdefault(table, []).append(path)
        with self._duck.transaction() as connection:
            restored_tables = set(by_table)
            present_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
            for table in (set(self.PUBLIC_DUCKDB_TABLES) - restored_tables) & present_tables:
                connection.execute(f"DELETE FROM {table}")
            for table, paths in by_table.items():
                connection.execute(f"DELETE FROM {table}")
                for parquet_file in paths:
                    source = str(parquet_file).replace("'", "''")
                    connection.execute(
                        f"INSERT INTO {table} BY NAME "
                        f"SELECT * FROM read_parquet('{source}')"
                    )

    def _snapshot_personalized_data(self) -> dict[str, list[dict[str, Any]]]:
        return {table: self._sqlite.query(f"SELECT * FROM {table}") for table in self.PERSONALIZED_TABLES}

    def _restore_personalized_data(self, data: dict[str, list[dict[str, Any]]]) -> None:
        with self._sqlite.transaction() as connection:
            for table in self._PERSONALIZED_DELETE_ORDER:
                connection.execute(f"DELETE FROM {table}")
            for table in self._PERSONALIZED_INSERT_ORDER:
                for row in data.get(table, []):
                    columns = list(row)
                    if not columns:
                        continue
                    placeholders = ", ".join("?" for _ in columns)
                    connection.execute(
                        f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                        [row[column] for column in columns],
                    )

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


def _file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_manifest(path: Path, root: Path) -> dict[str, Any]:
    del root
    return {"size_bytes": path.stat().st_size, "sha256": _file_checksum(path)}


def _directory_manifest(path: Path, root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    total_size = 0
    digest = hashlib.sha256()
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = file_path.relative_to(path).as_posix()
        checksum = _file_checksum(file_path)
        size = file_path.stat().st_size
        total_size += size
        files.append({"filename": relative, "size_bytes": size, "sha256": checksum})
        digest.update(f"{relative}\0{size}\0{checksum}\n".encode())
    return {"size_bytes": total_size, "sha256": digest.hexdigest(), "files": files}


def _manifest_authentication(manifest: dict[str, Any], data_key: bytes) -> str:
    unsigned = {key: value for key, value in manifest.items() if key != "authentication"}
    payload = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hmac.new(data_key, payload, hashlib.sha256).hexdigest()
