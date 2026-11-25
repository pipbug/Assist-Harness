from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .db import Category, Memory, SessionLocal, init_db
from .embeddings import content_hash, embed_text, try_embed
from .extract import extract_facts, ingest, start_backfill_thread
from .graph import build_graph
from .settings import settings

if not settings.session_secret or len(settings.session_secret) < 32:
    raise RuntimeError("SESSION_SECRET must be set (32+ chars)")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# cache-buster for /static, so a phone never mixes old CSS/JS with new pages
TEMPLATES.env.globals["asset_v"] = hashlib.sha256(
    b"".join(p.read_bytes() for p in sorted((Path(__file__).parent / "static").iterdir()) if p.is_file())
).hexdigest()[:10]
TAG_SPLIT = re.compile(r"[,\n]+")
SLUG_STRIP = re.compile(r"[^a-z0-9]+")
SOURCES = {"voice", "manual", "winston", "claude", "import"}
UI_PAGE = 100


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    start_backfill_thread()
    yield


app = FastAPI(title="Memory", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="memory_session",
    same_site="lax",
    https_only=True,
    max_age=60 * 60 * 24 * 30,
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


Db = Annotated[Session, Depends(get_db)]


# ---------- helpers ----------

def _parse_tags(tags: list[str] | str | None) -> list[str]:
    if tags is None:
        return []
    parts = TAG_SPLIT.split(tags) if isinstance(tags, str) else tags
    seen: list[str] = []
    for raw in parts:
        tag = " ".join(str(raw).split()).strip().lower()[:64]
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def _slugify(label: str) -> str:
    return SLUG_STRIP.sub("-", label.strip().lower()).strip("-")[:64]


def _memory_dict(m: Memory, score: float | None = None) -> dict:
    data = {
        "id": str(m.id),
        "user_id": m.user_id,
        "text": m.text,
        "memory": m.text,  # Mem0-compatible alias
        "category": m.category,
        "tags": list(m.tags or []),
        "source": m.source,
        "agent_id": m.agent_id,
        "metadata": m.metadata_json or {},
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
    if score is not None:
        data["score"] = round(score, 4)
    return data


def _valid_category(db: Session, slug: str | None) -> str | None:
    if not slug:
        return None
    slug = slug.strip().lower()
    if slug in ("", "none", "uncategorised", "uncategorized"):
        return None
    if not db.get(Category, slug):
        raise HTTPException(status_code=400, detail=f"Unknown category: {slug}")
    return slug


def _valid_source(source: str | None) -> str:
    source = (source or "manual").strip().lower()
    if source not in SOURCES:
        raise HTTPException(status_code=400, detail=f"source must be one of {sorted(SOURCES)}")
    return source


def _find_duplicate(db: Session, user_id: str, chash: str, exclude: uuid.UUID | None = None) -> Memory | None:
    stmt = select(Memory).where(Memory.user_id == user_id, Memory.content_hash == chash)
    if exclude:
        stmt = stmt.where(Memory.id != exclude)
    return db.scalars(stmt.limit(1)).first()


def _create_memory(
    db: Session,
    *,
    user_id: str,
    text: str,
    category: str | None,
    tags: list[str],
    source: str,
    agent_id: str | None = None,
    metadata: dict | None = None,
) -> tuple[Memory, bool]:
    """Insert a memory, or return the existing exact duplicate. Returns (memory, created)."""
    chash = content_hash(user_id, text)
    existing = _find_duplicate(db, user_id, chash)
    if existing:
        return existing, False
    memory = Memory(
        user_id=user_id,
        text=text,
        category=category,
        tags=tags,
        source=source,
        agent_id=agent_id,
        metadata_json=metadata or {},
        content_hash=chash,
        embedding=try_embed(text),
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory, True


def _ui_logged_in(request: Request) -> bool:
    return bool(request.session.get("auth"))


def require_api(request: Request) -> None:
    supplied = request.headers.get("x-api-key") or ""
    expected = settings.memory_api_key or ""
    if expected and hmac.compare_digest(supplied.encode(), expected.encode()):
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")


class _LoginRedirect(Exception):
    pass


def require_ui(request: Request) -> None:
    if not _ui_logged_in(request):
        raise _LoginRedirect()


@app.exception_handler(_LoginRedirect)
async def _login_redirect(_request: Request, _exc: _LoginRedirect):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


# ---------- API models ----------

class MemoryIn(BaseModel):
    """Either verbatim ``text`` or a conversation in ``messages`` to extract facts from."""

    model_config = ConfigDict(extra="ignore")

    text: str | None = None
    messages: list[dict] | None = None
    user_id: str | None = None
    category: str | None = None
    tags: list[str] | str = Field(default_factory=list)
    source: str | None = None
    agent_id: str | None = None
    metadata: dict = Field(default_factory=dict)
    # verbatim only: False skips near-duplicate handling (exact duplicates are still caught)
    dedup: bool = True
    # messages only: False stores each user message verbatim instead of extracting (Mem0's infer)
    infer: bool = True

    @model_validator(mode="after")
    def _one_of(self):
        if not (self.text and self.text.strip()) and not self.messages:
            raise ValueError("give text or messages")
        return self


class MemoryUpdate(BaseModel):
    text: str | None = None
    user_id: str | None = None
    category: str | None = None
    tags: list[str] | str | None = None
    source: str | None = None
    agent_id: str | None = None
    metadata: dict | None = None


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str = Field(..., min_length=1)
    user_id: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)
    limit: int | None = Field(default=None, ge=1, le=50)  # Mem0 alias of top_k
    category: str | None = None
    tags: list[str] | None = None
    threshold: float | None = Field(default=None, ge=-1, le=1)
    filters: dict | None = None  # Mem0 style: {"user_id": ..., "category": ..., "tags": [...]}


class CategoryIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=128)
    slug: str | None = None
    sort_order: int | None = None


class CategoryUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    sort_order: int | None = None


# ---------- API ----------

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def _cats(db: Session) -> list[Category]:
    return list(db.scalars(select(Category).order_by(Category.sort_order, Category.label)).all())


def _cat_dict(c: Category) -> dict:
    return {"slug": c.slug, "label": c.label, "sort_order": c.sort_order}


def _add_category(db: Session, label: str, slug: str | None, sort_order: int | None) -> Category:
    label = " ".join(label.split())
    slug = _slugify(slug or label)
    if not slug:
        raise HTTPException(status_code=400, detail="Category needs a name with letters or numbers")
    if db.get(Category, slug):
        raise HTTPException(status_code=409, detail=f"Category already exists: {slug}")
    if sort_order is None:
        sort_order = (db.scalar(select(func.max(Category.sort_order))) or 0) + 10
    cat = Category(slug=slug, label=label, sort_order=sort_order)
    db.add(cat)
    db.commit()
    return cat


def _delete_category(db: Session, slug: str) -> int:
    cat = db.get(Category, slug)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    moved = db.execute(update(Memory).where(Memory.category == slug).values(category=None)).rowcount
    db.delete(cat)
    db.commit()
    return moved or 0


@app.get("/categories")
def list_categories(db: Db, _: None = Depends(require_api)):
    return [_cat_dict(c) for c in _cats(db)]


@app.post("/categories", status_code=201)
def create_category(body: CategoryIn, db: Db, _: None = Depends(require_api)):
    return _cat_dict(_add_category(db, body.label, body.slug, body.sort_order))


@app.put("/categories/{slug}")
def update_category(slug: str, body: CategoryUpdate, db: Db, _: None = Depends(require_api)):
    cat = db.get(Category, slug)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    if body.label is not None:
        cat.label = " ".join(body.label.split())
    if body.sort_order is not None:
        cat.sort_order = body.sort_order
    db.commit()
    return _cat_dict(cat)


@app.delete("/categories/{slug}")
def delete_category(slug: str, db: Db, _: None = Depends(require_api)):
    moved = _delete_category(db, slug)
    return {"ok": True, "slug": slug, "uncategorised": moved}


@app.get("/memories")
def list_memories(
    db: Db,
    _: None = Depends(require_api),
    user_id: str = Query(default=settings.default_user_id),
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
        stmt = stmt.where(Memory.text.ilike(f"%{q.strip()}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Memory.updated_at.desc()).offset(offset).limit(limit)).all()
    return {"results": [_memory_dict(m) for m in rows], "total": total, "limit": limit, "offset": offset}


@app.post("/memories")
def add_memory(body: MemoryIn, db: Db, _: None = Depends(require_api)):
    user_id = (body.user_id or settings.default_user_id).strip()
    metadata = dict(body.metadata or {})
    source = _valid_source(body.source or metadata.pop("source", None))
    common = dict(
        user_id=user_id,
        category=_valid_category(db, body.category),
        tags=_parse_tags(body.tags),
        source=source,
        agent_id=body.agent_id,
        metadata=metadata,
    )
    if body.text and body.text.strip():
        res = ingest(db, text=body.text.strip(), use_llm=body.dedup, **common)
        memory = db.get(Memory, uuid.UUID(res["id"]))
        data = _memory_dict(memory)
        data["event"] = res["event"]
        if "previous_memory" in res:
            data["previous_memory"] = res["previous_memory"]
        return JSONResponse(data, status_code=201 if res["event"] == "ADD" else 200)

    if body.infer:
        try:
            facts = extract_facts(db, user_id, body.messages or [])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Extraction failed: {type(exc).__name__}") from exc
    else:
        facts = [
            {"text": " ".join(str(m.get("content") or "").split()), "category": None}
            for m in body.messages or []
            if m.get("role", "user") == "user" and str(m.get("content") or "").strip()
        ]
    results = []
    for fact in facts:
        args = dict(common)
        args["category"] = common["category"] or fact.get("category")
        results.append(ingest(db, text=fact["text"], **args))
    return {"results": results}


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
    if body.user_id is not None and body.user_id.strip() != memory.user_id:
        memory.user_id = body.user_id.strip()
        text_changed = True
    if body.category is not None:
        memory.category = _valid_category(db, body.category)
    if body.tags is not None:
        memory.tags = _parse_tags(body.tags)
    if body.source is not None:
        memory.source = _valid_source(body.source)
    if body.agent_id is not None:
        memory.agent_id = body.agent_id or None
    if body.metadata is not None:
        memory.metadata_json = body.metadata
    if text_changed:
        memory.content_hash = content_hash(memory.user_id, memory.text)
        if _find_duplicate(db, memory.user_id, memory.content_hash, exclude=memory.id):
            raise HTTPException(status_code=409, detail="An identical memory already exists")
        memory.embedding = try_embed(memory.text)
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
    filters = body.filters or {}
    user_id = str(body.user_id or filters.get("user_id") or settings.default_user_id).strip()
    category = body.category or filters.get("category")
    tags = body.tags or filters.get("tags")
    top_k = body.top_k or body.limit or 8
    try:
        query_vec = embed_text(body.query)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Embedding failed: {type(exc).__name__}") from exc
    distance = Memory.embedding.cosine_distance(query_vec).label("distance")
    stmt = select(Memory, distance).where(Memory.user_id == user_id, Memory.embedding.is_not(None))
    if category:
        stmt = stmt.where(Memory.category == category)
    for tag in _parse_tags(tags):
        stmt = stmt.where(Memory.tags.any(tag))
    rows = db.execute(stmt.order_by(distance).limit(top_k)).all()
    results = [_memory_dict(m, score=1.0 - float(d)) for m, d in rows]
    if body.threshold is not None:
        results = [r for r in results if r["score"] >= body.threshold]
    return {"results": results}


# ---------- UI ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _ui_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, password: Annotated[str, Form()] = ""):
    expected = settings.memory_ui_password or ""
    if expected and hmac.compare_digest(password.encode(), expected.encode()):
        request.session.clear()
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    time.sleep(1.0)  # slow down guessing
    return TEMPLATES.TemplateResponse(
        request, "login.html", {"error": "That password was not accepted."}, status_code=401
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def ui_list(
    request: Request,
    db: Db,
    _: None = Depends(require_ui),
    q: str = "",
    category: str = "",
    tag: str = "",
    page: int = Query(default=1, ge=1),
):
    user_id = settings.default_user_id
    stmt = select(Memory).where(Memory.user_id == user_id)
    if category == "_none":
        stmt = stmt.where(Memory.category.is_(None))
    elif category:
        stmt = stmt.where(Memory.category == category)
    if tag:
        stmt = stmt.where(Memory.tags.any(tag.strip().lower()))
    if q.strip():
        stmt = stmt.where(Memory.text.ilike(f"%{q.strip()}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    memories = db.scalars(
        stmt.order_by(Memory.updated_at.desc()).limit(UI_PAGE * page)
    ).all()
    counts = dict(
        db.execute(
            select(Memory.category, func.count()).where(Memory.user_id == user_id).group_by(Memory.category)
        ).all()
    )
    cats = _cats(db)
    return TEMPLATES.TemplateResponse(
        request,
        "list.html",
        {
            "memories": memories,
            "categories": cats,
            "labels": {c.slug: c.label for c in cats},
            "counts": counts,
            "all_count": sum(counts.values()),
            "q": q,
            "category": category,
            "tag": tag,
            "total": total,
            "page": page,
            "has_more": total > len(memories),
        },
    )


def _form(request: Request, db: Session, memory: Memory | None, error: str | None = None, **extra):
    ctx = {"memory": memory, "categories": _cats(db), "error": error}
    ctx.update(extra)
    return TEMPLATES.TemplateResponse(request, "form.html", ctx, status_code=400 if error else 200)


@app.get("/new", response_class=HTMLResponse)
def ui_new(request: Request, db: Db, _: None = Depends(require_ui), category: str = ""):
    return _form(request, db, None, form_category=category)


@app.post("/new")
def ui_new_submit(
    request: Request,
    db: Db,
    _: None = Depends(require_ui),
    text: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
):
    text = text.strip()
    if not text:
        return _form(request, db, None, "Please write a memory first.",
                     form_text=text, form_category=category, form_tags=tags)
    _create_memory(
        db,
        user_id=settings.default_user_id,
        text=text,
        category=_valid_category(db, category or None),
        tags=_parse_tags(tags),
        source="manual",
    )
    return RedirectResponse("/", status_code=303)


@app.get("/m/{memory_id}", response_class=HTMLResponse)
def ui_edit(request: Request, memory_id: uuid.UUID, db: Db, _: None = Depends(require_ui)):
    memory = db.get(Memory, memory_id)
    if not memory:
        return RedirectResponse("/", status_code=303)
    return _form(request, db, memory)


@app.post("/m/{memory_id}")
def ui_edit_submit(
    request: Request,
    memory_id: uuid.UUID,
    db: Db,
    _: None = Depends(require_ui),
    text: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
):
    memory = db.get(Memory, memory_id)
    if not memory:
        return RedirectResponse("/", status_code=303)
    text = text.strip()
    if not text:
        return _form(request, db, memory, "Please write a memory first.")
    if text != memory.text:
        chash = content_hash(memory.user_id, text)
        if _find_duplicate(db, memory.user_id, chash, exclude=memory.id):
            return _form(request, db, memory, "An identical memory already exists.")
        memory.text = text
        memory.content_hash = chash
        memory.embedding = try_embed(text)
    memory.category = _valid_category(db, category or None)
    memory.tags = _parse_tags(tags)
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


@app.get("/graph", response_class=HTMLResponse)
def ui_graph(request: Request, db: Db, _: None = Depends(require_ui)):
    return TEMPLATES.TemplateResponse(
        request, "graph.html", {"category_labels": {c.slug: c.label for c in _cats(db)}}
    )


@app.get("/graph/data")
def ui_graph_data(request: Request, db: Db):
    if not _ui_logged_in(request):  # fetched by script: say 401 instead of redirecting to the login page
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Log in first")
    return JSONResponse(build_graph(db, settings.default_user_id), headers={"Cache-Control": "no-store"})


def _cats_page(request: Request, db: Session, error: str | None = None):
    counts = dict(db.execute(select(Memory.category, func.count()).group_by(Memory.category)).all())
    return TEMPLATES.TemplateResponse(
        request,
        "categories.html",
        {"categories": _cats(db), "counts": counts, "error": error},
        status_code=400 if error else 200,
    )


@app.get("/c", response_class=HTMLResponse)
def ui_categories(request: Request, db: Db, _: None = Depends(require_ui)):
    return _cats_page(request, db)


@app.post("/c")
def ui_category_add(request: Request, db: Db, _: None = Depends(require_ui),
                    label: Annotated[str, Form()] = ""):
    try:
        _add_category(db, label, None, None)
    except HTTPException as exc:
        db.rollback()
        return _cats_page(request, db, str(exc.detail))
    return RedirectResponse("/c", status_code=303)


@app.post("/c/{slug}")
def ui_category_edit(request: Request, slug: str, db: Db, _: None = Depends(require_ui),
                     label: Annotated[str, Form()] = "", move: Annotated[str, Form()] = ""):
    cats = _cats(db)
    idx = next((i for i, c in enumerate(cats) if c.slug == slug), None)
    if idx is None:
        return RedirectResponse("/c", status_code=303)
    if move in ("up", "down"):
        j = idx - 1 if move == "up" else idx + 1
        if 0 <= j < len(cats):
            cats[idx], cats[j] = cats[j], cats[idx]
            for n, c in enumerate(cats):
                c.sort_order = (n + 1) * 10
    elif label.strip():
        cats[idx].label = " ".join(label.split())[:128]
    else:
        return _cats_page(request, db, "A category needs a name.")
    db.commit()
    return RedirectResponse("/c", status_code=303)


@app.post("/c/{slug}/delete")
def ui_category_delete(slug: str, db: Db, _: None = Depends(require_ui)):
    try:
        _delete_category(db, slug)
    except HTTPException:
        db.rollback()
    return RedirectResponse("/c", status_code=303)
