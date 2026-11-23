"""Phase 1: fact extraction from conversation turns, and near-duplicate handling on write.

Write path for every API add (extracted fact or verbatim text):
1. exact duplicate (content_hash)            -> NOOP
2. embed, nearest neighbours for this user
   - best similarity >= dedup_noop (0.99)    -> NOOP (practically identical text)
   - candidates >= dedup_candidate           -> LLM decides ADD / UPDATE / NOOP
   - otherwise                               -> ADD
UPDATE rewrites the existing row (merged or corrected text) instead of adding a new one, and
keeps the previous wording in metadata.history.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text as sql
from sqlalchemy.orm import Session

from .db import Category, Memory, SessionLocal
from .embeddings import clean_fact, content_hash, embed_text
from .llm import chat_json
from .settings import settings

log = logging.getLogger("memory.extract")
HISTORY_KEEP = 10
MAX_TURN_CHARS = 8000

DEFAULT_POLICY = (
    "Extract only durable facts about the user that will still be true and useful weeks from now "
    "(preferences, people, home, routines, plans, health, explicit 'remember that' requests, "
    "corrections). Ignore greetings, commands, device states, the date, time and weather, and "
    "anything the assistant said unless the user confirmed it. When in doubt, extract nothing."
)

EXTRACT_SYSTEM = """You extract long-term memories about {user} from a conversation with an assistant.
Each memory is one short third-person sentence. Output nothing for turns with no lasting information.

{policy}

CATEGORY: give each memory the best-fitting category slug from this list, or null if none fits:
{categories}
"""

RECONCILE_SYSTEM = """You maintain a long-term memory store about {user}. A NEW fact has arrived, and the store already
has the EXISTING memories listed below, which look similar. Decide how the new fact relates to them:

- NOOP: one existing memory already says the same thing (wording differences don't matter).
- UPDATE: the new fact is about the same specific subject as one existing memory and refines, extends or
  corrects it. Return the merged memory as one short third-person sentence: keep details from the old memory
  that are still valid, and prefer the new fact where they conflict (for a correction, you may note the old
  value in brackets, e.g. "(no longer Earl Grey)").
- ADD: the new fact is about a different subject from every existing memory. Facts that are only on a related
  topic (two different people, two different rooms, two different foods) are ADD.

Return target = the number of the existing memory for NOOP or UPDATE, or -1 for ADD.
For ADD and NOOP, text can be empty."""

RECONCILE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["ADD", "UPDATE", "NOOP"]},
        "target": {"type": "integer"},
        "text": {"type": "string"},
    },
    "required": ["action", "target", "text"],
    "additionalProperties": False,
}


def load_policy() -> str:
    try:
        policy = Path(settings.extraction_policy_path).read_text(encoding="utf-8").strip()
        if policy:
            return policy
    except OSError:
        pass
    return DEFAULT_POLICY


def _now() -> datetime:
    return datetime.now(timezone.utc)


def neighbours(db: Session, user_id: str, vec: list[float], k: int = 5, min_sim: float = 0.0,
               exclude=None) -> list[tuple[Memory, float]]:
    distance = Memory.embedding.cosine_distance(vec).label("distance")
    stmt = select(Memory, distance).where(Memory.user_id == user_id, Memory.embedding.is_not(None))
    if exclude is not None:
        stmt = stmt.where(Memory.id != exclude)
    rows = db.execute(stmt.order_by(distance).limit(k)).all()
    return [(m, 1.0 - float(d)) for m, d in rows if 1.0 - float(d) >= min_sim]


# ---------- extraction ----------

def _format_turn(messages: list[dict]) -> str:
    lines = []
    for msg in messages:
        role = str(msg.get("role") or "user").strip().lower()
        content = msg.get("content")
        if isinstance(content, list):  # OpenAI-style content parts
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        content = " ".join(str(content or "").split())
        if not content or role not in ("user", "assistant"):
            continue
        lines.append(f"{role.capitalize()}: {content}")
    return "\n".join(lines)[-MAX_TURN_CHARS:]


def extract_facts(db: Session, user_id: str, messages: list[dict]) -> list[dict]:
    turn = _format_turn(messages)
    if not turn:
        return []
    cats = list(db.scalars(select(Category).order_by(Category.sort_order)).all())
    slugs = [c.slug for c in cats]
    existing: list[str] = []
    try:
        existing = [m.text for m, _ in neighbours(db, user_id, embed_text(turn), k=8, min_sim=0.3)]
    except Exception as exc:  # noqa: BLE001 - context only; extraction still works without it
        log.warning("context lookup failed: %s", type(exc).__name__)
    schema = {
        "type": "object",
        "properties": {
            "memory": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "category": {"type": ["string", "null"], "enum": [*slugs, None]},
                    },
                    "required": ["id", "text", "category"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["memory"],
        "additionalProperties": False,
    }
    system = EXTRACT_SYSTEM.format(
        user=user_id.capitalize(),
        policy=load_policy(),
        categories="\n".join(f"- {c.slug}: {c.label}" for c in cats),
    )
    user = (
        "Existing Memories:\n"
        + ("\n".join(f"- {t}" for t in existing) if existing else "(none)")
        + "\n\nNew Messages:\n"
        + turn
    )
    out = chat_json(system, user, "memories", schema)
    facts = []
    for item in out.get("memory") or []:
        fact = clean_fact(str(item.get("text") or ""))
        if fact:
            cat = item.get("category")
            facts.append({"text": fact, "category": cat if cat in slugs else None})
    return facts


# ---------- reconcile and write ----------

def _reconcile(user_id: str, fact: str, candidates: list[tuple[Memory, float]]) -> tuple[str, int, str]:
    listing = "\n".join(f"{i}. {m.text}" for i, (m, _) in enumerate(candidates))
    out = chat_json(
        RECONCILE_SYSTEM.format(user=user_id.capitalize()),
        f"EXISTING:\n{listing}\n\nNEW: {fact}",
        "reconcile",
        RECONCILE_SCHEMA,
    )
    action, target = out.get("action"), out.get("target", -1)
    merged = clean_fact(str(out.get("text") or ""))
    if action in ("UPDATE", "NOOP") and not (isinstance(target, int) and 0 <= target < len(candidates)):
        action = "ADD"
    if action == "UPDATE" and not merged:
        merged = fact
    return action, target, merged


def _result(m: Memory, event: str, previous: str | None = None) -> dict:
    out = {"id": str(m.id), "memory": m.text, "event": event, "category": m.category}
    if previous is not None:
        out["previous_memory"] = previous
    return out


def ingest(
    db: Session,
    *,
    user_id: str,
    text: str,
    category: str | None = None,
    tags: list[str] | None = None,
    source: str = "manual",
    agent_id: str | None = None,
    metadata: dict | None = None,
    use_llm: bool = True,
) -> dict:
    """Write one fact with dedup. Commits. Returns {id, memory, event[, previous_memory]}."""
    tags = tags or []
    # serialise writes per user so two turns can't add the same fact at once
    db.execute(sql("SELECT pg_advisory_xact_lock(hashtext(:u))"), {"u": user_id})
    chash = content_hash(user_id, text)
    exact = db.scalars(select(Memory).where(Memory.user_id == user_id, Memory.content_hash == chash)).first()
    if exact:
        _merge_labels(exact, category, tags)
        db.commit()
        return _result(exact, "NOOP")

    try:
        vec = embed_text(text)
    except Exception as exc:  # noqa: BLE001 - save anyway; backfill will embed it later
        log.warning("embedding failed, adding without dedup: %s", type(exc).__name__)
        vec = None

    if vec is not None:
        near = neighbours(db, user_id, vec, k=5, min_sim=settings.dedup_candidate)
        if near and near[0][1] >= settings.dedup_noop:
            target = near[0][0]
            _merge_labels(target, category, tags)
            db.commit()
            return _result(target, "NOOP")
        if near and use_llm:
            try:
                action, idx, merged = _reconcile(user_id, text, near)
            except Exception as exc:  # noqa: BLE001 - fall back to a plain add
                log.warning("reconcile failed, adding: %s", type(exc).__name__)
                action, idx, merged = "ADD", -1, ""
            if action == "NOOP":
                target = near[idx][0]
                _merge_labels(target, category, tags)
                db.commit()
                return _result(target, "NOOP")
            if action == "UPDATE":
                return _apply_update(db, near[idx][0], merged, category, tags, source)

    memory = Memory(
        user_id=user_id,
        text=text,
        category=category,
        tags=tags,
        source=source,
        agent_id=agent_id,
        metadata_json=metadata or {},
        content_hash=chash,
        embedding=vec,
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return _result(memory, "ADD")


def _merge_labels(m: Memory, category: str | None, tags: list[str]) -> None:
    if category and not m.category:
        m.category = category
    if tags:
        new_tags = list(m.tags or []) + [t for t in tags if t not in (m.tags or [])]
        if new_tags != list(m.tags or []):
            m.tags = new_tags


def _apply_update(db: Session, target: Memory, merged: str, category, tags, source) -> dict:
    previous = target.text
    if merged == previous:
        _merge_labels(target, category, tags)
        db.commit()
        return _result(target, "NOOP")
    chash = content_hash(target.user_id, merged)
    clash = db.scalars(
        select(Memory).where(Memory.user_id == target.user_id, Memory.content_hash == chash,
                             Memory.id != target.id)
    ).first()
    if clash:
        db.commit()
        return _result(clash, "NOOP")
    meta = dict(target.metadata_json or {})
    history = list(meta.get("history") or [])
    history.append({"text": previous, "replaced_at": _now().isoformat(), "by": source})
    meta["history"] = history[-HISTORY_KEEP:]
    target.metadata_json = meta
    target.text = merged
    target.content_hash = chash
    try:
        target.embedding = embed_text(merged)
    except Exception:  # noqa: BLE001
        target.embedding = None
    _merge_labels(target, category, tags)
    target.updated_at = _now()
    db.commit()
    db.refresh(target)
    return _result(target, "UPDATE", previous)


# ---------- embedding backfill ----------

def backfill_once(batch: int = 50) -> int:
    done = 0
    with SessionLocal() as db:
        rows = db.scalars(select(Memory).where(Memory.embedding.is_(None)).limit(batch)).all()
        for m in rows:
            try:
                m.embedding = embed_text(m.text)
                done += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("backfill stopped: %s", type(exc).__name__)
                break
        db.commit()
    if done:
        log.warning("backfilled %d embeddings", done)
    return done


def start_backfill_thread() -> None:
    def loop():
        time.sleep(5)
        while True:
            try:
                while backfill_once() == 50:
                    pass
            except Exception as exc:  # noqa: BLE001
                log.warning("backfill error: %s", type(exc).__name__)
            time.sleep(max(60, settings.backfill_interval_s))

    threading.Thread(target=loop, name="embedding-backfill", daemon=True).start()
