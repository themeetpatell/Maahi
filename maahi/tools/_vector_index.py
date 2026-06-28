"""Local vector index over the Obsidian vault.

Strict-local design: embeddings come from Ollama (default model
``nomic-embed-text``). No data leaves the Mac. The index is persisted as
two files under ``<memory_dir>/vector_index/``:

  vectors.npy   — float32 array of shape (N, D)
  meta.json     — list of {path, mtime, sha, preview} aligned to rows

Updates are incremental and lazy: each ``update()`` call walks the vault,
re-embeds only notes whose content sha has changed, and drops rows for
notes that no longer exist. First run on a large vault may take a minute;
subsequent runs are near-instant.

Failure model: if Ollama is unreachable or the embedding model is not
pulled, the index degrades gracefully — searches raise a RuntimeError
that the calling tool can convert into a fallback grep.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from ..config import get_config

log = logging.getLogger(__name__)

EMBED_MODEL = "nomic-embed-text"
EMBED_DIM_GUESS = 768
MAX_CHARS_PER_NOTE = 4000
INDEX_DIR_NAME = "vector_index"
VECTORS_FILE = "vectors.npy"
META_FILE = "meta.json"


# ============================================================
#  EMBEDDING CLIENT
# ============================================================


class EmbeddingClient:
    """Thin wrapper around Ollama's /api/embeddings endpoint."""

    def __init__(self, host: str, model: str = EMBED_MODEL) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self._http = httpx.Client(
            base_url=self.host,
            timeout=httpx.Timeout(60.0, connect=5.0),
        )

    def embed(self, text: str) -> np.ndarray:
        """Return a 1-D float32 vector. Raises httpx.HTTPError on failure."""
        text = text or ""
        if len(text) > MAX_CHARS_PER_NOTE:
            text = text[:MAX_CHARS_PER_NOTE]
        r = self._http.post(
            "/api/embeddings",
            json={"model": self.model, "prompt": text},
        )
        r.raise_for_status()
        data = r.json()
        emb = data.get("embedding") or []
        if not emb:
            raise httpx.HTTPError("empty embedding returned")
        return np.asarray(emb, dtype=np.float32)


# ============================================================
#  INDEX
# ============================================================


@dataclass(frozen=True)
class IndexEntry:
    path: str       # relative to vault root
    mtime: float
    sha: str        # sha1(content)[:16]
    preview: str    # first 200 chars for display


class VaultIndex:
    """Persistent vector index over the Obsidian vault. Thread-safe."""

    def __init__(self) -> None:
        cfg = get_config()
        self.vault_root: Path = cfg.vault.path
        self.index_dir: Path = cfg.vault.memory_dir / INDEX_DIR_NAME
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self._vectors_path = self.index_dir / VECTORS_FILE
        self._meta_path = self.index_dir / META_FILE
        self._lock = threading.Lock()
        self._vectors: np.ndarray | None = None
        self._meta: list[IndexEntry] = []
        self._embedder = EmbeddingClient(cfg.brain.host)
        self._load()

    # ----- persistence -----

    def _load(self) -> None:
        if self._vectors_path.exists() and self._meta_path.exists():
            try:
                self._vectors = np.load(self._vectors_path)
                raw = json.loads(self._meta_path.read_text(encoding="utf-8"))
                self._meta = [IndexEntry(**e) for e in raw]
                log.info("Vector index loaded: %d notes", len(self._meta))
            except Exception as e:  # noqa: BLE001
                log.warning("Vector index corrupt, rebuilding: %s", e)
                self._vectors = None
                self._meta = []

    def _save(self) -> None:
        if self._vectors is None:
            return
        np.save(self._vectors_path, self._vectors)
        self._meta_path.write_text(
            json.dumps(
                [e.__dict__ for e in self._meta], ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    # ----- scan + embed -----

    def _vault_files(self) -> list[Path]:
        out: list[Path] = []
        for p in self.vault_root.rglob("*.md"):
            s = str(p)
            if "maahi/memory" in s or "/.git/" in s:
                continue
            out.append(p)
        return out

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]

    def update(self) -> dict[str, int]:
        with self._lock:
            return self._update_locked()

    def _update_locked(self) -> dict[str, int]:
        files = self._vault_files()
        by_path = {str(p.relative_to(self.vault_root)): p for p in files}

        existing: dict[str, tuple[int, IndexEntry]] = {
            e.path: (i, e) for i, e in enumerate(self._meta)
        }

        # Pass 1: decide which paths keep their existing row and which need re-embedding.
        plan: list[tuple[str, IndexEntry, np.ndarray | None]] = []
        changed = 0
        for rel, p in sorted(by_path.items()):
            try:
                mtime = p.stat().st_mtime
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            sha = self._sha(text)
            preview = text.strip().replace("\n", " ")[:200]

            prev = existing.get(rel)
            if prev is not None and prev[1].sha == sha:
                plan.append((rel, prev[1], None))
                continue

            try:
                vec = self._embedder.embed(text or rel)
            except httpx.HTTPError as e:
                log.warning("Embed failed for %s: %s", rel, e)
                if prev is not None:
                    plan.append((rel, prev[1], None))
                continue

            plan.append(
                (rel, IndexEntry(path=rel, mtime=mtime, sha=sha, preview=preview), vec),
            )
            changed += 1

        seen = {entry.path for _, entry, _ in plan}
        removed = sum(1 for p in existing if p not in seen)

        # Pass 2: stitch a vectors matrix that aligns row-for-row with plan order.
        if not plan:
            self._vectors = None
            self._meta = []
            self._save()
            return {"total": 0, "changed": 0, "removed": removed}

        dim = EMBED_DIM_GUESS
        if self._vectors is not None and self._vectors.size:
            dim = int(self._vectors.shape[1])
        for _, _, vec in plan:
            if vec is not None:
                dim = int(vec.shape[0])
                break

        stitched = np.zeros((len(plan), dim), dtype=np.float32)
        for i, (rel, entry, vec) in enumerate(plan):
            if vec is not None:
                stitched[i] = vec
            else:
                prev = existing.get(rel)
                if prev is not None and self._vectors is not None:
                    stitched[i] = self._vectors[prev[0]]
                else:
                    log.warning("Vector missing for %s — using zero row", rel)

        self._vectors = stitched
        self._meta = [entry for _, entry, _ in plan]
        self._save()
        log.info(
            "Vector index: %d total, %d changed, %d removed",
            len(self._meta), changed, removed,
        )
        return {"total": len(self._meta), "changed": changed, "removed": removed}

    # ----- search -----

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Return top-k hits ranked by cosine similarity."""
        with self._lock:
            if self._vectors is None or not self._meta:
                return []
            try:
                qv = self._embedder.embed(query)
            except httpx.HTTPError as e:
                raise RuntimeError(f"embedding query failed: {e}") from e

            mat = self._vectors
            mat_norm = np.linalg.norm(mat, axis=1, keepdims=True)
            mat_norm[mat_norm == 0] = 1.0
            mat_unit = mat / mat_norm
            q_norm = qv / max(float(np.linalg.norm(qv)), 1e-9)
            sims = mat_unit @ q_norm

            order = np.argsort(-sims)[:limit]
            return [
                {
                    "path": self._meta[int(i)].path,
                    "score": float(sims[int(i)]),
                    "preview": self._meta[int(i)].preview,
                }
                for i in order
            ]


# ============================================================
#  SINGLETON
# ============================================================


_INDEX: VaultIndex | None = None
_INDEX_LOCK = threading.Lock()


def get_index() -> VaultIndex:
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = VaultIndex()
        return _INDEX
