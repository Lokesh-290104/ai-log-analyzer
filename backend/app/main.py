"""HTTP API. Request and response bodies are Pydantic models: the contract the React app uses.

    POST /api/uploads              multipart "file" -> parse -> store -> overview (no LLM)
    POST /api/uploads/sample       same, with the built-in demo log
    GET  /api/uploads/{id}         overview + parse stats
    POST /api/uploads/{id}/ask     rate limited -> agent -> stored analysis
    GET  /api/uploads/{id}/analyses
    GET  /api/health
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text

from app.agent import Agent
from app.config import Settings
from app.db import Analysis, Database, EntryCache, Upload, load_entries, save_entries
from app.llm import LLMClient, LLMError, create_llm_client
from app.parser import ERROR_LEVELS, LogEntry, ParseError, parse_logs
from app.ratelimit import RateLimiter
from app.sample import generate_sample_logs
from app.tools import NoArgs, TimelineArgs, TopMessagesArgs, summary, timeline, top_messages

logger = logging.getLogger("log_analyzer")
READ_CHUNK = 256 * 1024
# Multipart framing around the file adds a little on top of the file itself.
MULTIPART_OVERHEAD = 64 * 1024


class UploadOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    size_bytes: int
    parse_stats: dict
    overview: dict


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)

    @field_validator("question")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("question is too short")
        return value


class AnalysisOut(BaseModel):
    id: str
    upload_id: str
    created_at: datetime
    question: str
    status: Literal["answered", "failed"]
    answer: str | None
    error_code: str | None
    error: str | None
    trace: list[dict]
    verification: dict | None
    model: str
    llm_calls: int
    duration_ms: int


def build_overview(entries: list[LogEntry]) -> dict:
    """Stats for the dashboard, computed by the agent's own tools so the numbers always agree."""
    return {
        **summary(entries, NoArgs()),
        "timeline": timeline(entries, TimelineArgs()),
        "top_errors": top_messages(entries, TopMessagesArgs(level=list(ERROR_LEVELS), limit=5)),
    }


def client_id(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.database_url)
        await db.create_tables()
        app.state.db = db
        yield
        await db.dispose()

    app = FastAPI(title="AI Log Analyzer", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.llm = llm
    app.state.cache = EntryCache(settings.entry_cache_entries)
    app.state.ask_limiter = RateLimiter(settings.ask_limit_per_minute, settings.ask_limit_per_day, noun="question")
    app.state.upload_limiter = RateLimiter(
        settings.upload_limit_per_minute, settings.upload_limit_per_day, noun="upload"
    )

    @app.middleware("http")
    async def reject_oversized_uploads(request: Request, call_next):
        # Refuse before the multipart body is read, when the client declares its size.
        if request.method == "POST" and request.url.path == "/api/uploads":
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes + MULTIPART_OVERHEAD:
                return JSONResponse({"detail": _too_large(settings)}, status_code=413)
        return await call_next(request)

    def get_llm() -> LLMClient:
        if app.state.llm is None:
            app.state.llm = create_llm_client(settings)  # raises LLMError when not configured
        return app.state.llm

    async def store_upload(name: str, raw: bytes) -> UploadOut:
        text_value = raw.decode("utf-8", errors="replace").lstrip("﻿")
        try:
            parsed = parse_logs(text_value, settings.max_upload_lines)
        except ParseError as e:
            raise HTTPException(400, str(e)) from None
        if not parsed.entries:
            raise HTTPException(
                400,
                "No log lines were recognized. Each entry needs a timestamp and a level "
                "(plain text, JSON lines or logfmt).",
            )
        upload = Upload(name=name[:200], size_bytes=len(raw), parse_stats=parsed.stats(),
                        overview=build_overview(parsed.entries))
        async with app.state.db.sessions() as session:
            session.add(upload)
            await session.flush()
            await save_entries(session, upload.id, parsed.entries)
            await session.commit()
        app.state.cache.put(upload.id, parsed.entries)
        return UploadOut.model_validate(upload, from_attributes=True)

    def check_limit(limiter: RateLimiter, request: Request) -> None:
        refusal = limiter.check(client_id(request, settings.trust_proxy))
        if refusal:
            raise HTTPException(429, refusal)

    async def get_upload(upload_id: str) -> Upload:
        async with app.state.db.sessions() as session:
            upload = await session.get(Upload, upload_id)
        if upload is None:
            raise HTTPException(404, "Upload not found.")
        return upload

    async def get_entries(upload_id: str) -> list[LogEntry]:
        entries = app.state.cache.get(upload_id)
        if entries is None:
            async with app.state.db.sessions() as session:
                entries = await load_entries(session, upload_id)
            app.state.cache.put(upload_id, entries)
        return entries

    async def recent_history(upload_id: str) -> list[tuple[str, str]]:
        if settings.history_turns <= 0:
            return []
        async with app.state.db.sessions() as session:
            rows = await session.execute(
                select(Analysis.question, Analysis.answer)
                .where(Analysis.upload_id == upload_id, Analysis.status == "answered")
                .order_by(Analysis.created_at.desc()).limit(settings.history_turns)
            )
            return [(q, a) for q, a in reversed(rows.all())]

    @app.get("/api/health")
    async def health():
        try:
            async with app.state.db.sessions() as session:
                await session.execute(text("SELECT 1"))
            database = "ok"
        except Exception:
            logger.exception("database health check failed")
            database = "error"
        return {"status": "ok" if database == "ok" else "degraded", "database": database,
                "llm_provider": settings.llm_provider}

    @app.post("/api/uploads", response_model=UploadOut, status_code=201)
    async def upload_logs(request: Request, file: UploadFile = File(...), name: str | None = Form(None)):
        check_limit(app.state.upload_limiter, request)
        chunks, size = [], 0
        while chunk := await file.read(READ_CHUNK):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                raise HTTPException(413, _too_large(settings))
            chunks.append(chunk)
        return await store_upload(name or file.filename or "upload.log", b"".join(chunks))

    @app.post("/api/uploads/sample", response_model=UploadOut, status_code=201)
    async def upload_sample(request: Request):
        check_limit(app.state.upload_limiter, request)
        return await store_upload("sample-shop-incident.log", generate_sample_logs().encode())

    @app.get("/api/uploads/{upload_id}", response_model=UploadOut)
    async def read_upload(upload_id: str):
        return UploadOut.model_validate(await get_upload(upload_id), from_attributes=True)

    @app.post("/api/uploads/{upload_id}/ask", response_model=AnalysisOut)
    async def ask(upload_id: str, body: AskIn, request: Request):
        upload = await get_upload(upload_id)
        check_limit(app.state.ask_limiter, request)
        try:
            llm = get_llm()
        except LLMError as e:
            raise HTTPException(503, str(e)) from None
        agent = Agent(llm, settings.max_agent_steps, settings.max_repairs, settings.agent_deadline_ms / 1000)
        history = await recent_history(upload_id)
        outcome = await agent.answer(body.question, await get_entries(upload_id), upload.overview, history)
        analysis = Analysis(
            upload_id=upload_id, question=body.question, status=outcome.status, answer=outcome.answer,
            error_code=outcome.error_code, error=outcome.error, trace=outcome.trace,
            verification=outcome.verification, model=outcome.model, llm_calls=outcome.llm_calls,
            duration_ms=outcome.duration_ms,
        )
        async with app.state.db.sessions() as session:
            session.add(analysis)
            await session.commit()
        if outcome.error_code == "llm_unavailable":
            raise HTTPException(503, outcome.error)
        # A fail-closed result is still a completed analysis: 200 with status "failed" and the trace.
        return AnalysisOut.model_validate(analysis, from_attributes=True)

    @app.get("/api/uploads/{upload_id}/analyses", response_model=list[AnalysisOut])
    async def list_analyses(upload_id: str):
        await get_upload(upload_id)
        async with app.state.db.sessions() as session:
            rows = await session.execute(
                select(Analysis).where(Analysis.upload_id == upload_id)
                .order_by(Analysis.created_at.desc()).limit(20)
            )
            return [AnalysisOut.model_validate(a, from_attributes=True) for a in rows.scalars()]

    if (settings.static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")

    return app


def _too_large(settings: Settings) -> str:
    return f"The file is too large. The limit is {settings.max_upload_bytes // 1_000_000} MB."


app = create_app()
