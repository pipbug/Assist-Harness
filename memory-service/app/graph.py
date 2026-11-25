"""Data for the memory graph page: memories, plus similarity links between them."""
from __future__ import annotations

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Memory

MAX_NODES = 300
TOP_K = 5
# Facts about one person all read alike ("Kira ..."), so unrelated pairs already score ~0.46 with
# text-embedding-3-small. Links below this floor are noise; the page starts higher (0.55).
MIN_SIM = 0.45


def similarity_links(vectors: list, top_k: int = TOP_K, min_sim: float = MIN_SIM) -> list[tuple[int, int, float]]:
    """Each vector's ``top_k`` nearest others at or above ``min_sim``, as unique (i, j, score), i < j."""
    if len(vectors) < 2:
        return []
    x = np.asarray(vectors, dtype=np.float32)
    x /= np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-9)
    sim = x @ x.T
    np.fill_diagonal(sim, -1.0)
    links: dict[tuple[int, int], float] = {}
    for i, row in enumerate(sim):
        for j in np.argsort(-row)[:top_k]:
            if row[j] < min_sim:
                break
            links[(min(i, int(j)), max(i, int(j)))] = round(min(float(row[j]), 1.0), 3)
    return sorted(((i, j, s) for (i, j), s in links.items()), key=lambda link: -link[2])


def build_graph(db: Session, user_id: str) -> dict:
    total = db.scalar(select(func.count()).select_from(Memory).where(Memory.user_id == user_id)) or 0
    rows = db.scalars(
        select(Memory).where(Memory.user_id == user_id).order_by(Memory.updated_at.desc()).limit(MAX_NODES)
    ).all()
    embedded = [m for m in rows if m.embedding is not None]
    links = similarity_links([m.embedding for m in embedded])
    return {
        "total": total,
        "memories": [
            {
                "id": str(m.id),
                "text": m.text,
                "category": m.category,
                "tags": list(m.tags or []),
                "source": m.source,
                "updated": m.updated_at.strftime("%B %-d %Y"),
            }
            for m in rows
        ],
        "links": [{"a": str(embedded[i].id), "b": str(embedded[j].id), "score": s} for i, j, s in links],
    }
