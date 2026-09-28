"""PostgreSQL (asyncpg) in production, SQLite (aiosqlite) for local runs and offline tests.

    uploads 1───* log_entries      one row per parsed entry (line number, UTC time, level, service)
    uploads 1───* analyses         question, status, answer, full tool trace, verification
"""

import uuid
from collections import OrderedDict
from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.parser import LogEntry

INSERT_CHUNK = 5000


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Upload(Base):
    __tablename__ = "uploads"

    # Random UUIDs: the public demo has no accounts, so ids must not be guessable.
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    size_bytes: Mapped[int] = mapped_column(Integer)
    parse_stats: Mapped[dict] = mapped_column(JSON)
    # Precomputed at upload by the same deterministic tools the agent uses: no LLM needed for stats.
    overview: Mapped[dict] = mapped_column(JSON)


class LogEntryRow(Base):
    __tablename__ = "log_entries"
    __table_args__ = (Index("ix_log_entries_upload_line", "upload_id", "line_no"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    upload_id: Mapped[str] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level: Mapped[str] = mapped_column(String(8))
    service: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    upload_id: Mapped[str] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # answered | failed
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace: Mapped[list] = mapped_column(JSON)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str] = mapped_column(String(100))
    llm_calls: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_tables(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


async def save_entries(session: AsyncSession, upload_id: str, entries: list[LogEntry]) -> None:
    rows = [
        {"upload_id": upload_id, "line_no": e.line_no, "ts": e.ts, "level": e.level,
         "service": e.service, "message": e.message, "detail": e.detail}
        for e in entries
    ]
    for i in range(0, len(rows), INSERT_CHUNK):
        await session.execute(insert(LogEntryRow), rows[i : i + INSERT_CHUNK])


async def load_entries(session: AsyncSession, upload_id: str) -> list[LogEntry]:
    result = await session.execute(
        select(LogEntryRow).where(LogEntryRow.upload_id == upload_id).order_by(LogEntryRow.line_no)
    )
    entries = [
        # SQLite drops the timezone on the way back; every stored time is UTC.
        LogEntry(r.line_no, r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=UTC), r.level, r.service, r.message,
                 r.detail or "")
        for r in result.scalars()
    ]
    entries.sort(key=lambda e: (e.ts, e.line_no))  # same order parse_logs produces
    return entries


class EntryCache:
    """LRU of parsed entries per upload, bounded by the TOTAL number of entries so a few
    100k-line uploads can't exhaust a small instance's memory. The database stays the
    source of truth; an evicted upload is reloaded on its next question."""

    def __init__(self, max_entries: int):
        self.max_entries = max_entries
        self._items: OrderedDict[str, list[LogEntry]] = OrderedDict()
        self._total = 0

    def get(self, upload_id: str) -> list[LogEntry] | None:
        entries = self._items.get(upload_id)
        if entries is not None:
            self._items.move_to_end(upload_id)
        return entries

    def put(self, upload_id: str, entries: list[LogEntry]) -> None:
        if len(entries) > self.max_entries:
            return
        if upload_id in self._items:
            self._total -= len(self._items.pop(upload_id))
        self._items[upload_id] = entries
        self._total += len(entries)
        while self._total > self.max_entries:
            _, evicted = self._items.popitem(last=False)
            self._total -= len(evicted)
