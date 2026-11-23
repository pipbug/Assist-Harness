from __future__ import annotations

import hmac
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .db import Category, Memory, SessionLocal, init_db
from .embeddings import content_hash, embed_text
from .settings import settings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TAG_SPLIT = re.compile(r"[,\n]+")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Memory", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or "change-me",
    session_cookie="memory_session",
    same_site="lax",
    https_only=True,
    max_age=60 * 60 * 24 * 14,
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


Db = Annotated[Session, Depends(get_db)]


def _parse_tags(tags: list[str] | str | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        parts = TAG_SPLIT.split(tags)
    else:
        parts = tags
    seen: list[str] = []
    for raw in parts:
        tag = " ".join(raw.split()).strip().lower()
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def _memory_dict(m: Memory, score: float | None = None) -> dict:
    data = {
        "id": str(m.id),
        "user_id": m.user_id,
        "text": m.text,
        "category": m.category,
        "tags": list(m.tags or []),
        "source": m.source,
        "agent_id": m.agent_id,
        "metadata": m.metadata_json or {},
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
    if score is not None:
        data["score"] = score
    return data


def _valid_category(db: Session, slug: str | None) -> str | None:
    if not slug:
        return None
    slug = slug.strip().lower()
    if slug in ("", "none", "uncategorised", "uncategorized"):
        return None
    found = db.get(Category, slug)
    if not found:
        raise HTTPException(status_code=400, detail=f"Unknown category: {slug}")
    return slug


def _ui_logged_in(request: Request) -> bool:
    return bool(request.session.get("auth"))


def require_api(request: Request) -> None:
    supplied = request.headers.get("x-api-key") or ""
    expected = settings.memory_api_key or ""
    if expected and hmac.compare_digest(supplied, expected):
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")


def require_ui(request: Request) -> None:
    if not _ui_logged_in(request):
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})


class MemoryIn(BaseModel):
    text: str = Field(..., min_length=1)
    user_id: str | None = None
    category: str | None = None
    tags: list[str] | str = Field(default_factory=list)
    source: str = "manual"
    agent_id: str | None = None
    metadata: dict = Field(default_factory=dict)


class MemoryUpdate(BaseModel):
    text: str | None = None
    user_id: str | None = None
    category: str | None = None
    tags: list[str] | str | None = None
    source: str | None = None
    agent_id: str | None = None
    metadata: dict | None = None


class SearchIn(BaseModel):
    query: str = Field(..., min_length=1)
    user_id: str | None = None
    top_k: int = Field(default=8, ge=1, le=50)
    category: str | None = None
    tags: list[str] | None = None


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/categories")
def list_categories(db: Db, _: None = Depends(require_api)):
    rows = db.scalars(select(Category).order_by(Category.sort_order, Category.label)).all()
    return [{"slug": c.slug, "label": c.label, "sort_order": c.sort_order} for c in rows]


@app.get("/memories")
def list_memories(
    db: Db,
    _: None = Depends(require_api),
    user_id: str = Query(default="kira"),
    category: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(Memory).where(Memory.user_id == user_id)
    if category:
        stmt = stmt.where(Memory.category == category)
    if tag:
        stmt = stmt.where(Memory.tags.any(tag.strip().lower()))
    if q:
        stmt = stmt.where(Memory.text.ilike(f"%{q}%"))
    stmt = stmt.order_by(Memory.updated_at.desc()).offset(offset).limit(limit)
    rows = db.scalars(stmt).all()
    return {"results": [_memory_dict(m) for m in rows]}


@app.post("/memories", status_code=201)
def add_memory(body: MemoryIn, db: Db, _: None = Depends(require_api)):
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    user_id = (body.user_id or settings.default_user_id).strip()
    category = _valid_category(db, body.category)
    tags = _parse_tags(body.tags)
    memory = Memory(
        user_id=user_id,
        text=text,
        category=category,
        tags=tags,
        source=body.source or "manual",
        agent_id=body.agent_id,
        metadata_json=body.metadata or {},
        content_hash=content_hash(user_id, text),
        embedding=embed_text(text),
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return _memory_dict(memory)


@app.get("/memories/{memory_id}")
def get_memory(memory_id: uuid.UUID, db: Db, _: None = Depends(require_api)):
    memory = db.get(Memory, memory_id)
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    return _memory_dict(memory)


@app.put("/memories/{memory_id}")
def update_memory(memory_id: uuid.UUID, body: MemoryUpdate, db: Db, _: None = Depends(require_api)):
    memory = db.get(Memory, memory_id)
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    text_changed = False
    if body.text is not None:
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="text cannot be empty")
        text_changed = text != memory.text
        memory.text = text
    if body.user_id is not None:
        memory.user_id = body.user_id.strip()
        text_changed = True
    if body.category is not None:
        memory.category = _valid_category(db, body.category)
    if body.tags is not None:
        memory.tags = _parse_tags(body.tags)
    if body.source is not None:
        memory.source = body.source
    if body.agent_id is not None:
        memory.agent_id = body.agent_id
    if body.metadata is not None:
        memory.metadata_json = body.metadata
    if text_changed:
        memory.content_hash = content_hash(memory.user_id, memory.text)
        memory.embedding = embed_text(memory.text)
    memory.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(memory)
    return _memory_dict(memory)


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: uuid.UUID, db: Db, _: None = Depends(require_api)):
    memory = db.get(Memory, memory_id)
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    db.delete(memory)
    db.commit()
    return {"ok": True, "id": str(memory_id)}


@app.post("/search")
def search_memories(body: SearchIn, db: Db, _: None = Depends(require_api)):
    user_id = (body.user_id or settings.default_user_id).strip()
    query_vec = embed_text(body.query)
    stmt = select(Memory).where(Memory.user_id == user_id, Memory.embedding.is_not(None))
    if body.category:
        stmt = stmt.where(Memory.category == body.category)
    if body.tags:
        for tag in _parse_tags(body.tags):
            stmt = stmt.where(Memory.tags.any(tag))
    stmt = stmt.order_by(Memory.embedding.cosine_distance(query_vec)).limit(body.top_k)
    rows = db.scalars(stmt).all()
    results = []
    for m in rows:
        # cosine distance = 1 - similarity; pgvector cosine_distance
        results.append(_memory_dict(m))
    return {"results": results}


# ---------- UI ----------

def _cats(db: Session) -> list[Category]:
    return list(db.scalars(select(Category).order_by(Category.sort_order, Category.label)).all())


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    if _ui_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"error": error},
    )


@app.post("/login")
def login_submit(request: Request, password: Annotated[str, Form()] = ""):
    expected = settings.memory_ui_password or ""
    if expected and hmac.compare_digest(password, expected):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"error": "That password was not accepted."},
        status_code=401,
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def ui_list(
    request: Request,
    db: Db,
    q: str = "",
    category: str = "",
    _: None = Depends(require_ui),
):
    stmt = select(Memory).where(Memory.user_id == settings.default_user_id)
    if category:
        stmt = stmt.where(Memory.category == category)
    if q:
        stmt = stmt.where(Memory.text.ilike(f"%{q.strip()}%"))
    stmt = stmt.order_by(Memory.updated_at.desc())
    memories = db.scalars(stmt).all()
    return TEMPLATES.TemplateResponse(
        request,
        "list.html",
        {
            "memories": memories,
            "categories": _cats(db),
            "q": q,
            "category": category,
        },
    )


@app.get("/new", response_class=HTMLResponse)
def ui_new(request: Request, db: Db, _: None = Depends(require_ui)):
    return TEMPLATES.TemplateResponse(
        request,
        "form.html",
        {"memory": None, "categories": _cats(db), "error": None},
    )


@app.post("/new")
def ui_new_submit(
    request: Request,
    db: Db,
    text: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    _: None = Depends(require_ui),
):
    text = text.strip()
    if not text:
        return TEMPLATES.TemplateResponse(
            request,
            "form.html",
            {
                "memory": None,
                "categories": _cats(db),
                "error": "Please write a memory first.",
                "form_text": text,
                "form_category": category,
                "form_tags": tags,
            },
            status_code=400,
        )
    memory = Memory(
        user_id=settings.default_user_id,
        text=text,
        category=_valid_category(db, category or None),
        tags=_parse_tags(tags),
        source="manual",
        content_hash=content_hash(settings.default_user_id, text),
        embedding=embed_text(text),
    )
    db.add(memory)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/m/{memory_id}", response_class=HTMLResponse)
def ui_edit(request: Request, memory_id: uuid.UUID, db: Db, _: None = Depends(require_ui)):
    memory = db.get(Memory, memory_id)
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    return TEMPLATES.TemplateResponse(
        request,
        "form.html",
        {"memory": memory, "categories": _cats(db), "error": None},
    )


@app.post("/m/{memory_id}")
def ui_edit_submit(
    request: Request,
    memory_id: uuid.UUID,
    db: Db,
    text: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    _: None = Depends(require_ui),
):
    memory = db.get(Memory, memory_id)
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    text = text.strip()
    if not text:
        return TEMPLATES.TemplateResponse(
            request,
            "form.html",
            {"memory": memory, "categories": _cats(db), "error": "Please write a memory first."},
            status_code=400,
        )
    text_changed = text != memory.text
    memory.text = text
    memory.category = _valid_category(db, category or None)
    memory.tags = _parse_tags(tags)
    if text_changed:
        memory.content_hash = content_hash(memory.user_id, memory.text)
        memory.embedding = embed_text(memory.text)
    memory.updated_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/m/{memory_id}/delete")
def ui_delete(memory_id: uuid.UUID, db: Db, _: None = Depends(require_ui)):
    memory = db.get(Memory, memory_id)
    if memory:
        db.delete(memory)
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == status.HTTP_303_SEE_OTHER and exc.headers and exc.headers.get("Location"):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith(("/login", "/", "/new", "/m")) and "text/html" in (
        request.headers.get("accept") or ""
    ):
        # fall through to default JSON for API clients
        if exc.status_code in (401, 403) and not request.url.path.startswith("/api"):
            pass
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
