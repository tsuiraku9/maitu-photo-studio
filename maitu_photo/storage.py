"""SQLite persistence for references, tasks, and per-chat continuity."""

from __future__ import annotations

import json
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

    SCHEMA_VERSION = 1

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
        self._initialize()

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
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 30000")
            self._connection.execute("PRAGMA synchronous = NORMAL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(
                """
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
            )
            self._connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run a transaction, using savepoints for nested storage operations."""

        self._ensure_open()
        with self._lock:
            depth = self._transaction_depth
            savepoint = f"maitu_sp_{depth}"
            try:
                if depth == 0:
                    self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                else:
                    self._connection.execute(f"SAVEPOINT {savepoint}")
                self._transaction_depth += 1
                yield self._connection
            except Exception:
                self._transaction_depth -= 1
                if depth == 0:
                    self._connection.rollback()
                else:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self._transaction_depth -= 1
                if depth == 0:
                    self._connection.commit()
                else:
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

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
