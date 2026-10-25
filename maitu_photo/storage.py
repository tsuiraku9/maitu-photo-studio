"""SQLite persistence for references, tasks, and per-chat continuity."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .models import (
    AssetStatus,
    GroupContinuity,
    ImageTask,
    ReferenceAsset,
    ReferenceCategory,
    TaskReference,
    TaskStatus,
    utc_now,
)


class StorageError(RuntimeError):
    pass


class DuplicateRecordError(StorageError):
    pass


class RecordNotFoundError(StorageError):
    pass


def _json_dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise StorageError("stored JSON value is not an object")
    return parsed


def _validate_request_id(request_id: str) -> None:
    pattern = r"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    if not isinstance(request_id, str) or re.fullmatch(pattern, request_id) is None:
        raise StorageError("request_id must be 32 lowercase hex characters or a canonical lowercase UUID")


def _request_response_json(response: dict[str, Any]) -> str:
    if not isinstance(response, dict):
        raise StorageError("request response must be a JSON object")
    try:
        return _json_dump(response)
    except (TypeError, ValueError) as exc:
        raise StorageError("request response must be JSON serializable") from exc


def _validate_pagination(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class SQLiteStorage:
    """Thread-safe storage backed by a single SQLite connection.

    The connection runs in autocommit mode.  Mutating methods create short
    `BEGIN IMMEDIATE` transactions, and nested method calls use savepoints.
    """

    SCHEMA_VERSION = 2

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._closed = False
        self._connection = sqlite3.connect(
            str(self.database_path),
            isolation_level=None,
            check_same_thread=False,
            timeout=30.0,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for diagnostics, not for application queries."""

        self._ensure_open()
        return self._connection

    def _ensure_open(self) -> None:
        if self._closed:
            raise StorageError("storage is closed")

    def _initialize(self) -> None:
        with self._lock:
            # Reject newer databases before changing any persistent settings.
            self._schema_version()
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 30000")
            self._connection.execute("PRAGMA synchronous = NORMAL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            with self.transaction() as connection:
                # Re-read under the write lock in case another connection migrated.
                version = self._schema_version()
                if version == 0:
                    self._create_base_schema(connection)
                    connection.execute("PRAGMA user_version = 1")
                    version = 1
                if version == 1:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS webui_requests (
                            request_id TEXT PRIMARY KEY NOT NULL,
                            parameter_hash TEXT NOT NULL,
                            operation TEXT NOT NULL,
                            state TEXT NOT NULL CHECK (
                                state IN ('preparing', 'accepted', 'completed', 'failed')
                            ),
                            task_id TEXT,
                            response_json TEXT,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    # No task FK: a receipt must outlive retained task metadata.
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS ix_webui_request_task
                            ON webui_requests(task_id) WHERE task_id IS NOT NULL
                        """
                    )
                    connection.execute("PRAGMA user_version = 2")

    def _schema_version(self) -> int:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 0 or version > self.SCHEMA_VERSION:
            raise StorageError(
                f"unsupported database schema version {version}; maximum supported is {self.SCHEMA_VERSION}"
            )
        return version

    @staticmethod
    def _create_base_schema(connection: sqlite3.Connection) -> None:
        schema = """
                CREATE TABLE IF NOT EXISTS reference_assets (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL CHECK (category IN ('person', 'outfit', 'scene')),
                    name TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    reference_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '{}',
                    manual_tags_json TEXT NOT NULL DEFAULT '{}',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (
                        status IN ('active', 'disabled', 'needs_review', 'deleted')
                    ),
                    source_task_id TEXT,
                    selection_metadata_json TEXT NOT NULL DEFAULT '{}',
                    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
                    last_used_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS ux_reference_category_hash
                    ON reference_assets(category, sha256)
                    WHERE deleted_at IS NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS ux_reference_person_singleton
                    ON reference_assets(category)
                    WHERE category = 'person' AND deleted_at IS NULL;
                CREATE INDEX IF NOT EXISTS ix_reference_selectable
                    ON reference_assets(category, status, updated_at)
                    WHERE deleted_at IS NULL;

                CREATE TABLE IF NOT EXISTS image_tasks (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    user_id TEXT,
                    stream_id TEXT,
                    group_id TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'generated', 'sent', 'failed', 'cancelled')
                    ),
                    prompt_summary TEXT NOT NULL DEFAULT '',
                    prompt_hash TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    request_json TEXT NOT NULL DEFAULT '{}',
                    result_path TEXT,
                    result_metadata_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT,
                    parent_task_id TEXT,
                    paid_request_started INTEGER NOT NULL DEFAULT 0,
                    sent_at TEXT,
                    planner_notified_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(parent_task_id) REFERENCES image_tasks(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS ix_task_queue
                    ON image_tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS ix_task_scope
                    ON image_tasks(scope_key, created_at DESC);
                CREATE INDEX IF NOT EXISTS ix_task_parent
                    ON image_tasks(parent_task_id);

                CREATE TABLE IF NOT EXISTS task_references (
                    task_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    asset_id TEXT,
                    selection_source TEXT NOT NULL,
                    fallback_reason TEXT,
                    selection_metadata_json TEXT NOT NULL DEFAULT '{}',
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, role),
                    FOREIGN KEY(task_id) REFERENCES image_tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(asset_id) REFERENCES reference_assets(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS group_continuity (
                    scope_key TEXT PRIMARY KEY,
                    local_date TEXT NOT NULL,
                    scene_signature TEXT NOT NULL,
                    last_photo_at TEXT NOT NULL,
                    outfit_id TEXT,
                    scene_id TEXT,
                    pinned_outfit_id TEXT,
                    pinned_scene_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(outfit_id) REFERENCES reference_assets(id) ON DELETE SET NULL,
                    FOREIGN KEY(scene_id) REFERENCES reference_assets(id) ON DELETE SET NULL,
                    FOREIGN KEY(pinned_outfit_id) REFERENCES reference_assets(id) ON DELETE SET NULL,
                    FOREIGN KEY(pinned_scene_id) REFERENCES reference_assets(id) ON DELETE SET NULL
                );
                """
        # executescript implicitly commits; execute each static DDL statement
        # separately so v0 -> v1 -> v2 is one atomic migration.
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run a transaction, using savepoints for nested storage operations."""

        with self._lock:
            self._ensure_open()
            depth = self._transaction_depth
            savepoint = f"maitu_sp_{depth}"
            if depth == 0:
                self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            else:
                self._connection.execute(f"SAVEPOINT {savepoint}")
            self._transaction_depth = depth + 1
            try:
                yield self._connection
                if depth == 0:
                    self._connection.commit()
                else:
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                if depth == 0:
                    self._connection.rollback()
                else:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            finally:
                self._transaction_depth = depth

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # WebUI request receipts ------------------------------------------

    def begin_request(self, request_id: str, operation: str, parameter_hash: str) -> dict[str, Any]:
        """Persist an idempotency receipt, not an authorization or claim token.

        An existing matching request is returned unchanged.  The caller must
        serialize preparation/submission; this DTO does not elect an owner.
        """

        _validate_request_id(request_id)
        if not isinstance(operation, str) or not operation.strip():
            raise StorageError("operation must be a non-empty string")
        if not isinstance(parameter_hash, str) or re.fullmatch(r"[0-9a-f]{64}", parameter_hash) is None:
            raise StorageError("parameter_hash must be a lowercase SHA-256 digest")
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM webui_requests WHERE request_id = ?", (request_id,)).fetchone()
            if row is not None:
                if row["operation"] != operation or row["parameter_hash"] != parameter_hash:
                    raise StorageError("request_id was already used for a different operation or parameters")
                return self._request_from_row(row)
            now = _timestamp(utc_now())
            connection.execute(
                """
                INSERT INTO webui_requests (
                    request_id, parameter_hash, operation, state,
                    task_id, response_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'preparing', NULL, NULL, ?, ?)
                """,
                (request_id, parameter_hash, operation, now, now),
            )
            row = connection.execute("SELECT * FROM webui_requests WHERE request_id = ?", (request_id,)).fetchone()
            return self._request_from_row(row)

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        _validate_request_id(request_id)
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM webui_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            return self._request_from_row(row) if row is not None else None

    def update_request(
        self,
        request_id: str,
        *,
        state: str,
        response: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Update a receipt; omitted response/task_id preserve their saved values."""

        _validate_request_id(request_id)
        if state not in ("preparing", "accepted", "completed", "failed"):
            raise StorageError("invalid request state")
        response_json = _request_response_json(response) if response is not None else None
        if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
            raise StorageError("task_id must be a non-empty string or None")
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE webui_requests
                SET state = ?, response_json = COALESCE(?, response_json),
                    task_id = COALESCE(?, task_id), updated_at = ?
                WHERE request_id = ?
                """,
                (state, response_json, task_id, _timestamp(utc_now()), request_id),
            )
            if cursor.rowcount != 1:
                raise RecordNotFoundError(f"request not found: {request_id}")
            row = connection.execute("SELECT * FROM webui_requests WHERE request_id = ?", (request_id,)).fetchone()
            return self._request_from_row(row)

    def complete_requests_for_task(self, task_id: str, response: dict[str, Any]) -> None:
        """Finalize pending receipts atomically with a service's task transaction.

        A response with ``success is False`` records failure.  Other responses
        record completion; terminal receipts are left unchanged on callbacks.
        """

        if not isinstance(task_id, str) or not task_id.strip():
            raise StorageError("task_id must be a non-empty string")
        response_json = _request_response_json(response)
        state = "failed" if response.get("success") is False else "completed"
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE webui_requests SET state = ?, response_json = ?, updated_at = ?
                WHERE task_id = ? AND state IN ('preparing', 'accepted')
                """,
                (state, response_json, _timestamp(utc_now()), task_id),
            )

    # Reference assets -------------------------------------------------

    def create_reference_asset(self, asset: ReferenceAsset) -> ReferenceAsset:
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO reference_assets (
                        id, category, name, source_path, reference_path, sha256,
                        tags_json, manual_tags_json, prompt_version, status,
                        source_task_id, selection_metadata_json, use_count,
                        last_used_at, created_at, updated_at, deleted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset.id,
                        asset.category.value,
                        asset.name,
                        str(asset.source_path),
                        str(asset.reference_path),
                        asset.sha256,
                        _json_dump(asset.tags),
                        _json_dump(asset.manual_tags),
                        asset.prompt_version,
                        asset.status.value,
                        asset.source_task_id,
                        _json_dump(asset.selection_metadata),
                        asset.use_count,
                        _timestamp(asset.last_used_at),
                        _timestamp(asset.created_at),
                        _timestamp(asset.updated_at),
                        _timestamp(asset.deleted_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateRecordError(str(exc)) from exc
        return asset

    def get_reference_asset(self, asset_id: str, *, include_deleted: bool = False) -> ReferenceAsset | None:
        self._ensure_open()
        sql = "SELECT * FROM reference_assets WHERE id = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        with self._lock:
            row = self._connection.execute(sql, (asset_id,)).fetchone()
        return self._asset_from_row(row) if row else None

    def find_reference_by_hash(
        self,
        category: ReferenceCategory | str,
        sha256: str,
        *,
        include_deleted: bool = False,
    ) -> ReferenceAsset | None:
        category_value = ReferenceCategory(category).value
        sql = "SELECT * FROM reference_assets WHERE category = ? AND sha256 = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        sql += " ORDER BY created_at DESC LIMIT 1"
        with self._lock:
            row = self._connection.execute(sql, (category_value, sha256.lower().strip())).fetchone()
        return self._asset_from_row(row) if row else None

    def list_reference_assets(
        self,
        *,
        category: ReferenceCategory | str | None = None,
        statuses: Iterable[AssetStatus | str] | None = None,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReferenceAsset]:
        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        if offset < 0:
            raise ValueError("offset must not be negative")
        clauses: list[str] = []
        values: list[Any] = []
        if category is not None:
            clauses.append("category = ?")
            values.append(ReferenceCategory(category).value)
        if statuses is not None:
            status_values = [AssetStatus(status).value for status in statuses]
            if not status_values:
                return []
            placeholders = ",".join("?" for _ in status_values)
            clauses.append(f"status IN ({placeholders})")
            values.extend(status_values)
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend((limit, offset))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM reference_assets" + where + " ORDER BY created_at DESC, id ASC LIMIT ? OFFSET ?",
                values,
            ).fetchall()
        return [self._asset_from_row(row) for row in rows]

    def query_reference_assets(
        self,
        category: ReferenceCategory | str | None = None,
        status: AssetStatus | str | None = None,
        search: str = "",
        limit: int = 12,
        offset: int = 0,
    ) -> tuple[list[ReferenceAsset], int]:
        """Return a stable page and its filtered total, excluding deleted by default."""

        _validate_pagination(limit, offset)
        clauses: list[str] = []
        values: list[Any] = []
        if category not in (None, ""):
            clauses.append("category = ?")
            values.append(ReferenceCategory(category).value)
        if status not in (None, ""):
            status_value = AssetStatus(status).value
            clauses.append("status = ?")
            values.append(status_value)
            if status_value != AssetStatus.DELETED.value:
                clauses.append("deleted_at IS NULL")
        else:
            clauses.extend(("deleted_at IS NULL", "status != 'deleted'"))
        if not isinstance(search, str):
            raise ValueError("search must be a string")
        if search.strip():
            escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(name LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\' "
                "OR tags_json LIKE ? ESCAPE '\\' OR manual_tags_json LIKE ? ESCAPE '\\')"
            )
            values.extend((pattern, pattern, pattern, pattern))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.transaction(immediate=False) as connection:
            total = connection.execute("SELECT COUNT(*) FROM reference_assets" + where, values).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM reference_assets" + where + " ORDER BY created_at DESC, id ASC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        return [self._asset_from_row(row) for row in rows], total

    def file_is_referenced(self, path: Path | str, exclude_asset_id: str = "") -> bool:
        """Protect shared live paths and active reference-job targets, not history.

        ``exclude_asset_id`` excludes that asset's live reference only; an active
        job targeting it still protects the file.  Paths are compared lexically,
        without inspecting the filesystem or following symlinks.
        """

        stored_path = str(Path(path))
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT 1 FROM reference_assets AS asset
                WHERE (asset.source_path = ? OR asset.reference_path = ?)
                  AND (
                    (asset.id != ? AND asset.deleted_at IS NULL AND asset.status != 'deleted')
                    OR EXISTS (
                        SELECT 1 FROM image_tasks AS task
                        WHERE task.kind LIKE 'reference\\_%' ESCAPE '\\'
                          AND task.status IN ('queued', 'running', 'generated')
                          AND CASE WHEN json_valid(task.request_json)
                              THEN json_extract(task.request_json, '$.asset_id') END = asset.id
                    )
                  )
                LIMIT 1
                """,
                (stored_path, stored_path, exclude_asset_id),
            ).fetchone()
        return row is not None

    def update_reference_asset(self, asset: ReferenceAsset) -> ReferenceAsset:
        asset.updated_at = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE reference_assets SET
                    category = ?, name = ?, source_path = ?, reference_path = ?,
                    sha256 = ?, tags_json = ?, manual_tags_json = ?,
                    prompt_version = ?, status = ?, source_task_id = ?,
                    selection_metadata_json = ?, use_count = ?, last_used_at = ?,
                    updated_at = ?, deleted_at = ?
                WHERE id = ?
                """,
                (
                    asset.category.value,
                    asset.name,
                    str(asset.source_path),
                    str(asset.reference_path),
                    asset.sha256,
                    _json_dump(asset.tags),
                    _json_dump(asset.manual_tags),
                    asset.prompt_version,
                    asset.status.value,
                    asset.source_task_id,
                    _json_dump(asset.selection_metadata),
                    asset.use_count,
                    _timestamp(asset.last_used_at),
                    _timestamp(asset.updated_at),
                    _timestamp(asset.deleted_at),
                    asset.id,
                ),
            )
            if cursor.rowcount != 1:
                raise RecordNotFoundError(f"reference asset not found: {asset.id}")
        return asset

    def set_reference_status(self, asset_id: str, status: AssetStatus | str) -> ReferenceAsset:
        asset = self.get_reference_asset(asset_id, include_deleted=True)
        if asset is None:
            raise RecordNotFoundError(f"reference asset not found: {asset_id}")
        new_status = AssetStatus(status)
        asset.status = new_status
        if new_status == AssetStatus.DELETED:
            asset.deleted_at = utc_now()
        elif asset.deleted_at is not None:
            raise StorageError("a soft-deleted reference cannot be re-enabled")
        return self.update_reference_asset(asset)

    def soft_delete_reference_asset(self, asset_id: str) -> ReferenceAsset:
        return self.set_reference_status(asset_id, AssetStatus.DELETED)

    def increment_reference_usage(self, asset_ids: Iterable[str], *, used_at: datetime | None = None) -> None:
        unique_ids = tuple(dict.fromkeys(asset_ids))
        if not unique_ids:
            return
        used_at = used_at or utc_now()
        placeholders = ",".join("?" for _ in unique_ids)
        with self.transaction() as connection:
            connection.execute(
                f"""
                UPDATE reference_assets
                SET use_count = use_count + 1, last_used_at = ?, updated_at = ?
                WHERE id IN ({placeholders}) AND deleted_at IS NULL
                """,
                (_timestamp(used_at), _timestamp(used_at), *unique_ids),
            )

    def reference_is_protected(self, asset_id: str) -> bool:
        """Return whether an asset is held by an active task or continuity state."""

        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT 1
                WHERE EXISTS (
                    SELECT 1 FROM task_references AS ref
                    JOIN image_tasks AS task ON task.id = ref.task_id
                    WHERE ref.asset_id = ? AND task.status IN ('queued', 'running', 'generated')
                )
                OR EXISTS (
                    SELECT 1 FROM group_continuity AS state
                    WHERE state.outfit_id = ? OR state.scene_id = ?
                       OR state.pinned_outfit_id = ? OR state.pinned_scene_id = ?
                )
                LIMIT 1
                """,
                (asset_id, asset_id, asset_id, asset_id, asset_id),
            ).fetchone()
        return row is not None

    # Tasks ------------------------------------------------------------

    def create_task(self, task: ImageTask) -> ImageTask:
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO image_tasks (
                        id, kind, scope_key, user_id, stream_id, group_id, status,
                        prompt_summary, prompt_hash, prompt_version, request_json,
                        result_path, result_metadata_json, error_message,
                        parent_task_id, paid_request_started, sent_at,
                        planner_notified_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._task_values(task),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateRecordError(str(exc)) from exc
        return task

    def get_task(self, task_id: str) -> ImageTask | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM image_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    def update_task(self, task: ImageTask) -> ImageTask:
        task.updated_at = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE image_tasks SET
                    kind = ?, scope_key = ?, user_id = ?, stream_id = ?,
                    group_id = ?, status = ?, prompt_summary = ?, prompt_hash = ?,
                    prompt_version = ?, request_json = ?, result_path = ?,
                    result_metadata_json = ?, error_message = ?, parent_task_id = ?,
                    paid_request_started = ?, sent_at = ?, planner_notified_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    task.kind,
                    task.scope_key,
                    task.user_id,
                    task.stream_id,
                    task.group_id,
                    task.status.value,
                    task.prompt_summary,
                    task.prompt_hash,
                    task.prompt_version,
                    _json_dump(task.request),
                    str(task.result_path) if task.result_path else None,
                    _json_dump(task.result_metadata),
                    task.error_message,
                    task.parent_task_id,
                    int(task.paid_request_started),
                    _timestamp(task.sent_at),
                    _timestamp(task.planner_notified_at),
                    _timestamp(task.updated_at),
                    task.id,
                ),
            )
            if cursor.rowcount != 1:
                raise RecordNotFoundError(f"task not found: {task.id}")
        return task

    def set_task_status(
        self,
        task_id: str,
        status: TaskStatus | str,
        *,
        result_path: Path | str | None = None,
        result_metadata: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> ImageTask:
        task = self.get_task(task_id)
        if task is None:
            raise RecordNotFoundError(f"task not found: {task_id}")
        task.status = TaskStatus(status)
        if result_path is not None:
            task.result_path = Path(result_path)
        if result_metadata is not None:
            task.result_metadata = dict(result_metadata)
        task.error_message = error_message
        if task.status == TaskStatus.SENT and task.sent_at is None:
            task.sent_at = utc_now()
        return self.update_task(task)

    def list_tasks(
        self,
        *,
        scope_key: str | None = None,
        statuses: Iterable[TaskStatus | str] | None = None,
        parent_task_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ImageTask]:
        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        clauses: list[str] = []
        values: list[Any] = []
        if scope_key is not None:
            clauses.append("scope_key = ?")
            values.append(scope_key)
        if statuses is not None:
            status_values = [TaskStatus(status).value for status in statuses]
            if not status_values:
                return []
            clauses.append("status IN (" + ",".join("?" for _ in status_values) + ")")
            values.extend(status_values)
        if parent_task_id is not None:
            clauses.append("parent_task_id = ?")
            values.append(parent_task_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend((limit, offset))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM image_tasks" + where + " ORDER BY created_at DESC, id ASC LIMIT ? OFFSET ?",
                values,
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def query_reference_tasks(
        self,
        origin: str = "",
        status: TaskStatus | str = "",
        limit: int = 12,
        offset: int = 0,
    ) -> tuple[list[ImageTask], int]:
        """Query reference jobs, including legacy jobs without an explicit origin."""

        _validate_pagination(limit, offset)
        clauses = ["kind LIKE 'reference\\_%' ESCAPE '\\'"]
        values: list[Any] = []
        if origin:
            clauses.append(
                """
                COALESCE(NULLIF(json_extract(request_json, '$.origin'), ''),
                    CASE
                        WHEN json_extract(request_json, '$.automatic') = 1 THEN 'automatic'
                        WHEN COALESCE(stream_id, '') != '' THEN 'qq'
                        ELSE 'unknown'
                    END
                ) = ?
                """
            )
            values.append(origin)
        if status:
            clauses.append("status = ?")
            values.append(TaskStatus.SENT.value if status == "completed" else TaskStatus(status).value)
        where = " WHERE " + " AND ".join(clauses)
        with self.transaction(immediate=False) as connection:
            total = connection.execute("SELECT COUNT(*) FROM image_tasks" + where, values).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM image_tasks" + where + " ORDER BY created_at DESC, id ASC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        return [self._task_from_row(row) for row in rows], total

    def latest_task(self, scope_key: str) -> ImageTask | None:
        tasks = self.list_tasks(scope_key=scope_key, limit=1)
        return tasks[0] if tasks else None

    def delete_task(self, task_id: str) -> bool:
        """Delete retained task metadata after its configured retention period."""

        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM image_tasks WHERE id = ?", (task_id,))
            return cursor.rowcount == 1

    def claim_next_task(self, *, kinds: Sequence[str] | None = None) -> ImageTask | None:
        clauses = ["status = 'queued'"]
        values: list[Any] = []
        if kinds:
            clauses.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
            values.extend(kinds)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM image_tasks WHERE " + " AND ".join(clauses) + " ORDER BY created_at ASC, id ASC LIMIT 1",
                values,
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE image_tasks SET status = 'running', updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (_timestamp(now), row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = dict(row)
            claimed["status"] = TaskStatus.RUNNING.value
            claimed["updated_at"] = _timestamp(now)
            return self._task_from_row(claimed)

    def mark_task_request_started(self, task_id: str) -> ImageTask:
        task = self.get_task(task_id)
        if task is None:
            raise RecordNotFoundError(f"task not found: {task_id}")
        task.paid_request_started = True
        return self.update_task(task)

    def mark_task_planner_notified(self, task_id: str, *, notified_at: datetime | None = None) -> ImageTask:
        task = self.get_task(task_id)
        if task is None:
            raise RecordNotFoundError(f"task not found: {task_id}")
        task.planner_notified_at = notified_at or utc_now()
        return self.update_task(task)

    def recover_interrupted_tasks(self) -> tuple[list[str], list[str]]:
        """Recover pre-request tasks and quarantine ambiguous paid requests.

        Returns `(requeued_ids, failed_ids)`.  Tasks with a persisted result are
        always requeued for delivery, even if a crash happened after the paid
        request flag was set.  Only running tasks with no result and an already
        started paid request are quarantined to avoid duplicate billing.
        """

        now = utc_now()
        with self.transaction() as connection:
            requeued = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM image_tasks
                    WHERE status = 'generated'
                       OR (status = 'running' AND (paid_request_started = 0 OR result_path IS NOT NULL))
                    """
                ).fetchall()
            ]
            failed = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM image_tasks
                    WHERE status = 'running' AND paid_request_started = 1 AND result_path IS NULL
                    """
                ).fetchall()
            ]
            connection.execute(
                """
                UPDATE image_tasks SET status = 'queued', updated_at = ?
                WHERE status = 'generated'
                   OR (status = 'running' AND (paid_request_started = 0 OR result_path IS NOT NULL))
                """,
                (_timestamp(now),),
            )
            connection.execute(
                """
                UPDATE image_tasks
                SET status = 'failed',
                    error_message = 'interrupted after provider request; manual retry required',
                    updated_at = ?
                WHERE status = 'running' AND paid_request_started = 1 AND result_path IS NULL
                """,
                (_timestamp(now),),
            )
        return requeued, failed

    def record_task_reference(self, reference: TaskReference) -> TaskReference:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO task_references (
                    task_id, role, asset_id, selection_source, fallback_reason,
                    selection_metadata_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, role) DO UPDATE SET
                    asset_id = excluded.asset_id,
                    selection_source = excluded.selection_source,
                    fallback_reason = excluded.fallback_reason,
                    selection_metadata_json = excluded.selection_metadata_json,
                    recorded_at = excluded.recorded_at
                """,
                (
                    reference.task_id,
                    reference.role,
                    reference.asset_id,
                    reference.selection_source,
                    reference.fallback_reason,
                    _json_dump(reference.selection_metadata),
                    _timestamp(reference.recorded_at),
                ),
            )
        return reference

    def list_task_references(self, task_id: str) -> list[TaskReference]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM task_references WHERE task_id = ? ORDER BY role ASC",
                (task_id,),
            ).fetchall()
        return [self._task_reference_from_row(row) for row in rows]

    # Continuity -------------------------------------------------------

    def get_continuity(self, scope_key: str) -> GroupContinuity | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM group_continuity WHERE scope_key = ?", (scope_key,)
            ).fetchone()
        return self._continuity_from_row(row) if row else None

    def upsert_continuity(self, state: GroupContinuity) -> GroupContinuity:
        state.updated_at = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO group_continuity (
                    scope_key, local_date, scene_signature, last_photo_at,
                    outfit_id, scene_id, pinned_outfit_id, pinned_scene_id,
                    metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    local_date = excluded.local_date,
                    scene_signature = excluded.scene_signature,
                    last_photo_at = excluded.last_photo_at,
                    outfit_id = excluded.outfit_id,
                    scene_id = excluded.scene_id,
                    pinned_outfit_id = excluded.pinned_outfit_id,
                    pinned_scene_id = excluded.pinned_scene_id,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.scope_key,
                    state.local_date,
                    state.scene_signature,
                    _timestamp(state.last_photo_at),
                    state.outfit_id,
                    state.scene_id,
                    state.pinned_outfit_id,
                    state.pinned_scene_id,
                    _json_dump(state.metadata),
                    _timestamp(state.updated_at),
                ),
            )
        return state

    def delete_continuity(self, scope_key: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM group_continuity WHERE scope_key = ?", (scope_key,))
            return cursor.rowcount == 1

    # Row mappers ------------------------------------------------------

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "request_id": row["request_id"],
            "state": row["state"],
            "operation": row["operation"],
            "parameter_hash": row["parameter_hash"],
            "task_id": row["task_id"],
            "response": _json_load(row["response_json"]) if row["response_json"] is not None else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _asset_from_row(row: sqlite3.Row | dict[str, Any]) -> ReferenceAsset:
        return ReferenceAsset(
            id=row["id"],
            category=row["category"],
            name=row["name"],
            source_path=row["source_path"],
            reference_path=row["reference_path"],
            sha256=row["sha256"],
            tags=_json_load(row["tags_json"]),
            manual_tags=_json_load(row["manual_tags_json"]),
            prompt_version=row["prompt_version"],
            status=row["status"],
            source_task_id=row["source_task_id"],
            selection_metadata=_json_load(row["selection_metadata_json"]),
            use_count=row["use_count"],
            last_used_at=_datetime(row["last_used_at"]),
            created_at=_datetime(row["created_at"]) or utc_now(),
            updated_at=_datetime(row["updated_at"]) or utc_now(),
            deleted_at=_datetime(row["deleted_at"]),
        )

    @staticmethod
    def _task_values(task: ImageTask) -> tuple[Any, ...]:
        return (
            task.id,
            task.kind,
            task.scope_key,
            task.user_id,
            task.stream_id,
            task.group_id,
            task.status.value,
            task.prompt_summary,
            task.prompt_hash,
            task.prompt_version,
            _json_dump(task.request),
            str(task.result_path) if task.result_path else None,
            _json_dump(task.result_metadata),
            task.error_message,
            task.parent_task_id,
            int(task.paid_request_started),
            _timestamp(task.sent_at),
            _timestamp(task.planner_notified_at),
            _timestamp(task.created_at),
            _timestamp(task.updated_at),
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row | dict[str, Any]) -> ImageTask:
        return ImageTask(
            id=row["id"],
            kind=row["kind"],
            scope_key=row["scope_key"],
            user_id=row["user_id"],
            stream_id=row["stream_id"],
            group_id=row["group_id"],
            status=row["status"],
            prompt_summary=row["prompt_summary"],
            prompt_hash=row["prompt_hash"],
            prompt_version=row["prompt_version"],
            request=_json_load(row["request_json"]),
            result_path=row["result_path"],
            result_metadata=_json_load(row["result_metadata_json"]),
            error_message=row["error_message"],
            parent_task_id=row["parent_task_id"],
            paid_request_started=bool(row["paid_request_started"]),
            sent_at=_datetime(row["sent_at"]),
            planner_notified_at=_datetime(row["planner_notified_at"]),
            created_at=_datetime(row["created_at"]) or utc_now(),
            updated_at=_datetime(row["updated_at"]) or utc_now(),
        )

    @staticmethod
    def _task_reference_from_row(row: sqlite3.Row) -> TaskReference:
        return TaskReference(
            task_id=row["task_id"],
            role=row["role"],
            asset_id=row["asset_id"],
            selection_source=row["selection_source"],
            fallback_reason=row["fallback_reason"],
            selection_metadata=_json_load(row["selection_metadata_json"]),
            recorded_at=_datetime(row["recorded_at"]) or utc_now(),
        )

    @staticmethod
    def _continuity_from_row(row: sqlite3.Row) -> GroupContinuity:
        return GroupContinuity(
            scope_key=row["scope_key"],
            local_date=row["local_date"],
            scene_signature=row["scene_signature"],
            last_photo_at=_datetime(row["last_photo_at"]) or utc_now(),
            outfit_id=row["outfit_id"],
            scene_id=row["scene_id"],
            pinned_outfit_id=row["pinned_outfit_id"],
            pinned_scene_id=row["pinned_scene_id"],
            metadata=_json_load(row["metadata_json"]),
            updated_at=_datetime(row["updated_at"]) or utc_now(),
        )
