"""PDF 管理 — 下载/归档/恢复 (PRD §14 SD9, §17, §18.1-18.2)

CNINFO 公告 PDF 下载: static.cninfo.com.cn/{adjunctUrl}
冷归档: 热数据(data/pdf/) → 冷归档(用户配置目录)
恢复: 冷归档 → 热数据 (CLI, 两段式确认)
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Config
from app.core.storage.maintenance import MaintenanceLockError, exclusive_maintenance
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

_PDF_BASE = "https://static.cninfo.com.cn"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.cninfo.com.cn/",
}


class PDFManager:
    """PDF 下载/归档/恢复管理器"""

    def __init__(
        self,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
    ) -> None:
        if sqlite is None and paths is None:
            raise PathIsolationError("PDFManager requires a SQLite store or validated paths")
        if paths is not None:
            validated = paths.validate()
            sqlite = sqlite or SQLiteStore(paths=validated)
            if sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("PDFManager store does not match injected paths")

        assert sqlite is not None
        cfg = Config.current()
        self._project_root = cfg.project_root
        self._sqlite = sqlite
        # Test/profile-owned PDFs must never resolve through the repository root.
        self._data_root = (
            self._project_root / "data"
            if sqlite.paths.env.value == "formal"
            else sqlite.paths.run_root / "data"
        )
        self._hot_dir = self._data_root / "pdf"
        self._archive_root = self._resolve_archive_root(None)

    @property
    def hot_dir(self) -> Path:
        return self._hot_dir

    @property
    def archive_root(self) -> Path:
        return self._archive_root

    def _stock_dir(self, stock_code: str, *, create: bool = False) -> Path:
        if not re.fullmatch(r"\d{6}", stock_code):
            raise PathIsolationError("Invalid stock code")
        hot_root = self._hot_dir.resolve()
        stock_dir = (hot_root / stock_code).resolve()
        try:
            stock_dir.relative_to(hot_root)
        except ValueError as error:
            raise PathIsolationError("PDF path escapes hot_dir") from error
        if create:
            stock_dir.mkdir(parents=True, exist_ok=True)
        return stock_dir

    def _resolve_archive_root(self, requested_root: str | None) -> Path:
        pdf_config = Config.current().get_value("pdf", {})
        configured_root = pdf_config.get("archive_root") if isinstance(pdf_config, dict) else None
        if not isinstance(configured_root, str) or not configured_root:
            raise PathIsolationError("pdf.archive_root must be configured")

        data_root = self._data_root.resolve()
        configured = (data_root / configured_root).resolve()
        try:
            configured.relative_to(data_root)
        except ValueError as error:
            raise PathIsolationError("pdf.archive_root must remain under the profile data root") from error

        if requested_root is None:
            return configured
        requested = Path(requested_root)
        if requested.is_absolute():
            raise PathIsolationError("Archive target must be the configured relative root")
        if requested.parts[:1] == ("data",):
            requested = Path(*requested.parts[1:])
        candidate = (data_root / requested).resolve()
        if candidate != configured:
            raise PathIsolationError("Archive target must match pdf.archive_root")
        return configured

    def download_pdf(
        self,
        stock_code: str,
        adjunct_url: str,
        announcement_id: str | None = None,
    ) -> dict[str, Any]:
        """下载 CNINFO 公告 PDF (PRD §14 SD9: 已恢复本地PDF的浏览器打开)

        Args:
            stock_code: 股票代码
            adjunct_url: CNINFO adjunctUrl (如 /finalpage/2026-07-15/123456.PDF)
            announcement_id: 公告ID (可选, 用于溯源)

        Returns:
            下载结果, 含本地路径和文件哈希
        """
        if not adjunct_url:
            return {"error": "adjunct_url is required"}
        try:
            stock_dir = self._stock_dir(stock_code, create=True)
        except PathIsolationError as error:
            return {"error": str(error)}

        url = f"{_PDF_BASE}/{adjunct_url.lstrip('/')}"

        # 从 URL 提取文件名
        filename = Path(
            adjunct_url.rsplit("/", 1)[-1] if "/" in adjunct_url else adjunct_url
        ).name
        if not filename or filename in {".", ".."}:
            return {"error": "invalid PDF filename"}
        if not filename.endswith(".pdf"):
            filename += ".pdf"

        local_path = stock_dir / filename

        try:
            with httpx.Client(timeout=60.0, headers=_HEADERS) as client:
                resp = client.get(url)
                resp.raise_for_status()

                content = resp.content
            # P2修复: Content-Type验证
            ct = resp.headers.get("content-type", "")
            if "pdf" not in ct and "octet-stream" not in ct:
                return {"error": f"非PDF内容: {ct}"}
            if len(content) > 50 * 1024 * 1024:
                return {"error": "文件过大(>50MB)"}

            try:
                with exclusive_maintenance(self._sqlite.db_path):
                    with tempfile.NamedTemporaryFile(
                        dir=stock_dir, prefix=f".{filename}.", delete=False,
                    ) as temporary:
                        temporary.write(content)
                        temporary_path = Path(temporary.name)
                    temporary_path.replace(local_path)
            except MaintenanceLockError as error:
                return {"error": str(error), "stock_code": stock_code, "url": url}
            pdf_hash = hashlib.sha256(content).hexdigest()

            logger.info(f"PDF 下载成功: {stock_code} → {local_path} ({len(content)} bytes)")

            return {
                "status": "ok",
                "stock_code": stock_code,
                "filename": filename,
                "local_path": str(local_path),
                "size_bytes": len(content),
                "pdf_hash": pdf_hash,
                "announcement_id": announcement_id,
            }

        except httpx.HTTPError as e:
            logger.error(f"PDF 下载失败: {stock_code} {url}: {e}")
            return {"error": str(e), "stock_code": stock_code, "url": url}

    def download_announcement_pdfs(
        self,
        stock_code: str,
        category: str | None = None,
        max_count: int = 5,
    ) -> dict[str, Any]:
        """批量下载某股票的公告 PDF

        通过 CNINFO 适配器获取公告列表, 然后下载 PDF。
        """
        from app.core.adapters.base import FetchRequest
        from app.core.adapters.manager import AdapterManager

        mgr = AdapterManager()
        result = mgr.fetch(FetchRequest(
            data_type="announcements",
            stock_codes=[stock_code],
            extra_params={"category": category} if category else {},
        ))

        if result.metadata.error or not result.data:
            return {"error": result.metadata.error or "no announcements", "count": 0}

        downloaded = []
        for ann in result.data[:max_count]:
            adjunct_url = ann.get("adjunct_url")
            if not adjunct_url:
                continue

            dl = self.download_pdf(
                stock_code=stock_code,
                adjunct_url=adjunct_url,
                announcement_id=ann.get("announcement_id"),
            )
            if "error" not in dl:
                downloaded.append(dl)

        return {
            "status": "ok",
            "stock_code": stock_code,
            "downloaded": len(downloaded),
            "files": downloaded,
        }

    def list_local_pdfs(self, stock_code: str) -> list[dict[str, Any]]:
        """列出本地已下载的 PDF"""
        try:
            stock_dir = self._stock_dir(stock_code)
        except PathIsolationError:
            return []
        if not stock_dir.exists():
            return []

        files = []
        for f in stock_dir.glob("*.pdf"):
            stat = f.stat()
            files.append({
                "filename": f.name,
                "size_bytes": stat.st_size,
                "path": str(f),
            })
        return files

    def archive_pdfs(
        self,
        stock_code: str | None = None,
        target_dir: str | None = None,
    ) -> dict[str, Any]:
        """归档 PDF 到冷存储 (PRD §18.1 AR1: 热数据→冷归档)

        Args:
            stock_code: 指定股票 (None=全部)
            target_dir: 冷归档目标目录
        """
        if stock_code is not None and not re.fullmatch(r"\d{6}", stock_code):
            return {"error": "Invalid stock code"}
        try:
            archive_root = self._resolve_archive_root(target_dir)
        except PathIsolationError as error:
            return {"error": str(error)}
        archive_root.mkdir(parents=True, exist_ok=True)

        hot_root = self._hot_dir.resolve()
        try:
            with exclusive_maintenance(self._sqlite.db_path):
                return self._archive_pdfs_locked(stock_code, archive_root, hot_root)
        except MaintenanceLockError as error:
            return {"error": str(error)}

    def _archive_pdfs_locked(self, stock_code: str | None, archive_root: Path, hot_root: Path) -> dict[str, Any]:
        archived = []
        if stock_code:
            stock_dirs = [self._hot_dir / stock_code]
        else:
            stock_dirs = [d for d in self._hot_dir.iterdir() if d.is_dir()] if self._hot_dir.exists() else []
        for stock_dir in stock_dirs:
            if not stock_dir.exists():
                continue
            source_stock_dir = stock_dir.resolve()
            code = stock_dir.name
            if not re.fullmatch(r"\d{6}", code):
                return {"error": "Invalid stock code"}
            try:
                source_stock_dir.relative_to(hot_root)
            except ValueError:
                return {"error": "PDF archive source escapes hot_dir"}
            target_stock_dir = (archive_root / code).resolve()
            try:
                target_stock_dir.relative_to(archive_root)
            except ValueError:
                return {"error": "Archive destination escapes the configured root"}
            target_stock_dir.mkdir(parents=True, exist_ok=True)

            for pdf_file in stock_dir.glob("*.pdf"):
                source_path = pdf_file.resolve()
                try:
                    source_path.relative_to(source_stock_dir)
                    source_path.relative_to(hot_root)
                except ValueError:
                    return {"error": "PDF archive source escapes hot_dir"}
                target_path = (target_stock_dir / pdf_file.name).resolve()
                try:
                    target_path.relative_to(archive_root)
                except ValueError:
                    return {"error": "Archive destination escapes the configured root"}
                # P2修复: copy2+verify+delete而非move（防止跨文件系统copy失败后原文件丢失）
                checksum = self._checksum(source_path)
                source_size = source_path.stat().st_size
                shutil.copy2(str(source_path), str(target_path))
                if (
                    not target_path.exists()
                    or target_path.stat().st_size != source_size
                    or self._checksum(target_path) != checksum
                ):
                    return {"error": f"归档验证失败: {pdf_file.name}"}
                with self._sqlite.transaction() as conn:
                    conn.execute(
                        """INSERT INTO pdf_archive_manifest
                           (stock_code, filename, archive_path, checksum)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(stock_code, filename) DO UPDATE SET
                             archive_path=excluded.archive_path, checksum=excluded.checksum,
                             archived_at=CURRENT_TIMESTAMP""",
                        [code, pdf_file.name, str(target_path.resolve()), checksum],
                    )
                # Recheck the source after copying: never delete a replacement
                # that appeared while the archive was being verified.
                if source_path.stat().st_size != source_size or self._checksum(source_path) != checksum:
                    return {"error": f"PDF changed during archive: {pdf_file.name}"}
                source_path.unlink()
                archived.append({
                    "stock_code": code,
                    "filename": pdf_file.name,
                    "archive_path": str(target_path),
                })

        # Keep the archive-root entry for backup inventory; each file's manifest
        # above is the authoritative recovery location and checksum.
        with self._sqlite.transaction() as conn:
            conn.execute(
                "INSERT INTO backup_registry (type, path, checksum, encrypted) VALUES (?, ?, ?, ?)",
                ["archive", str(archive_root), self._checksum_tree(archive_root), False],
            )

        logger.info(f"PDF 归档完成: {len(archived)} 个文件 → {archive_root}")
        return {
            "status": "ok",
            "archived_count": len(archived),
            "files": archived,
            "archive_dir": str(archive_root),
        }

    def restore_pdf(
        self,
        stock_code: str,
        filename: str,
        archive_dir: str | None = None,
    ) -> dict[str, Any]:
        """从冷归档恢复 PDF (PRD §18.2 AR5-AR7: 恢复只能通过CLI)

        Args:
            stock_code: 股票代码
            filename: PDF 文件名
            archive_dir: 冷归档目录
        """
        import re

        if not re.match(r"^\d{6}$", stock_code):
            return {"error": "Invalid stock code"}
        if not re.match(r"^[a-zA-Z0-9_.\-]+\.pdf$", filename, re.IGNORECASE):
            return {"error": "Invalid filename"}
        if ".." in filename or "/" in filename or "\\" in filename:
            return {"error": "Invalid filename"}

        try:
            archive_root = self._resolve_archive_root(archive_dir)
        except PathIsolationError as error:
            return {"error": str(error)}

        try:
            with exclusive_maintenance(self._sqlite.db_path):
                return self._restore_pdf_locked(stock_code, filename, archive_root)
        except MaintenanceLockError as error:
            return {"error": str(error)}

    def _restore_pdf_locked(self, stock_code: str, filename: str, archive_root: Path) -> dict[str, Any]:
        manifest = self._sqlite.query(
            "SELECT archive_path, checksum FROM pdf_archive_manifest WHERE stock_code = ? AND filename = ?",
            [stock_code, filename],
        )
        if manifest:
            archive_path = Path(manifest[0]["archive_path"]).resolve()
            expected_checksum = manifest[0]["checksum"]
        else:
            archive_path = (archive_root / stock_code / filename).resolve()
            expected_checksum = None

        try:
            archive_path.relative_to(archive_root)
        except ValueError:
            return {"error": "Path traversal detected"}

        if not archive_path.exists():
            return {"error": f"PDF not found in archive: {archive_path}"}
        if expected_checksum and self._checksum(archive_path) != expected_checksum:
            return {"error": "archive checksum verification failed"}

        hot_dir_resolved = self._hot_dir.resolve()
        hot_path = (self._hot_dir / stock_code / filename).resolve()
        try:
            hot_path.relative_to(hot_dir_resolved)
        except ValueError:
            return {"error": "Path traversal detected"}

        hot_path.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(str(archive_path), str(hot_path))
        if expected_checksum and self._checksum(hot_path) != expected_checksum:
            hot_path.unlink(missing_ok=True)
            return {"error": "restored PDF checksum verification failed"}

        logger.info(f"PDF 恢复: {archive_path} → {hot_path}")
        return {
            "status": "ok",
            "stock_code": stock_code,
            "filename": filename,
            "restored_to": str(hot_path),
        }

    def get_pdf_path(self, stock_code: str, filename: str) -> Path | None:
        """获取 PDF 路径 (先查热数据, 再查冷归档)

        PRD §18.2 AR6: 网页需要的PDF尚在冷归档中时, 显示归档位置与恢复指引
        """
        hot_path = self._hot_dir / stock_code / filename
        if hot_path.exists():
            return hot_path

        if self.is_in_archive(stock_code, filename):
            return None  # 在冷归档中, 需要恢复

        return None  # 不存在

    def is_in_archive(self, stock_code: str, filename: str) -> bool:
        """检查 PDF 是否在冷归档中"""
        rows = self._sqlite.query(
            "SELECT archive_path, checksum FROM pdf_archive_manifest WHERE stock_code = ? AND filename = ?",
            [stock_code, filename],
        )
        return bool(rows and Path(rows[0]["archive_path"]).exists() and self._checksum(Path(rows[0]["archive_path"])) == rows[0]["checksum"])

    def list_archived_pdfs(self, stock_code: str) -> list[dict[str, Any]]:
        """Return manifest-backed archive entries that remain checksum-valid."""
        files: list[dict[str, Any]] = []
        for row in self._sqlite.query(
            "SELECT filename, archive_path, checksum FROM pdf_archive_manifest WHERE stock_code = ?",
            [stock_code],
        ):
            path = Path(row["archive_path"])
            if path.exists() and self._checksum(path) == row["checksum"]:
                files.append({
                    "filename": row["filename"], "size_bytes": path.stat().st_size,
                    "archived": True, "archive_path": str(path), "checksum": row["checksum"],
                    "integrity_verified": True,
                })
        return files

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _checksum_tree(self, root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*.pdf")):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(self._checksum(path).encode())
        return digest.hexdigest()

    def record_parse_failure(
        self,
        stock_code: str,
        announcement_id: str | None = None,
        pdf_hash: str | None = None,
        page: int | None = None,
        error: str = "",
    ) -> None:
        """记录 PDF 解析失败任务 (PRD §17: 生成可机器处理的失败任务)"""
        with self._sqlite.transaction() as conn:
            conn.execute(
                """INSERT INTO pdf_tasks
                   (stock_code, announcement_id, pdf_hash, page, error, status)
                   VALUES (?, ?, ?, ?, ?, 'pending')""",
                [stock_code, announcement_id, pdf_hash, page, error[:500]],
            )
        logger.info(f"PDF 解析失败任务已记录: {stock_code}")

    def list_parse_failures(self, limit: int = 50) -> list[dict[str, Any]]:
        """列出 PDF 解析失败任务"""
        return self._sqlite.query(
            "SELECT * FROM pdf_tasks WHERE status = 'pending' ORDER BY id DESC LIMIT ?",
            [limit],
        )
