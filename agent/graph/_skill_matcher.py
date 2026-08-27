"""Fail-open semantic hints for skills in the live Capabilities index.

The user-message path never waits for corpus embeddings. A missing or stale
on-disk cache skips the current hint and starts one daemon-thread rebuild;
subsequent turns load the fingerprint-keyed vectors and spend only the bounded
query embedding plus a small in-memory cosine search.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from langchain_core.messages import HumanMessage

from agent.graph._capabilities import _one_line, indexed_skills
from agent.messages import NoteTag, system_note_message
from ava.security import is_flagged
from shared.config.turn_view import turn_settings
from shared.log import logger
from shared.private_storage import write_private_bytes

_CACHE_SCHEMA_VERSION = 1
# Mirror the memory indexer's proven batch size so a large installed catalog
# does not become one oversized Gemini request during a background rebuild.
_EMBED_BATCH_SIZE = 32


@dataclass(frozen=True)
class _SkillSnapshot:
    identifier: str
    target: str
    name: str
    description: str
    mtime_ns: int

    @property
    def corpus_text(self) -> str:
        return f"{self.name}\n{self.identifier}\n{self.description}"


@dataclass(frozen=True)
class _SkillVectorCache:
    fingerprint: str
    skills: tuple[_SkillSnapshot, ...]
    vectors: np.ndarray


_CacheKey = tuple[Path, str]
_memory_caches: dict[_CacheKey, _SkillVectorCache] = {}
_rebuild_threads: dict[_CacheKey, threading.Thread] = {}
_rebuild_lock = threading.Lock()


def _cache_dir() -> Path:
    """Per-unit cache directory, following the SDK's `$AVA_HOME/*_cache` convention."""
    from shared.paths import ava_home

    return ava_home() / "skill_match_cache"


def _safe_skill_snapshot() -> tuple[_SkillSnapshot, ...]:
    """Live Capabilities skills whose descriptions may safely enter a hint."""
    import ava

    snapshots: list[_SkillSnapshot] = []
    for skill in indexed_skills():
        description = skill["description"]
        if is_flagged(description):
            continue
        snapshots.append(
            _SkillSnapshot(
                identifier=ava.skills.identifier(skill),
                target=ava.skills.target(skill),
                name=skill["name"],
                description=_one_line(description),
                mtime_ns=(Path(skill["path"]) / "SKILL.md").stat().st_mtime_ns,
            )
        )
    return tuple(sorted(snapshots, key=lambda skill: skill.identifier))


def _fingerprint(skills: tuple[_SkillSnapshot, ...]) -> str:
    payload = {
        "schema": _CACHE_SCHEMA_VERSION,
        "skills": [asdict(skill) for skill in skills],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _cache_path(fingerprint: str) -> Path:
    return _cache_dir() / f"v{_CACHE_SCHEMA_VERSION}-{fingerprint}.npz"


def _normalized_vectors(vectors: Any, expected_rows: int) -> np.ndarray:
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] != expected_rows or matrix.shape[1] == 0:
        raise ValueError(
            f"skill embedding shape {matrix.shape} does not match {expected_rows} skills"
        )
    norms = np.linalg.norm(matrix, axis=1)
    if not np.isfinite(matrix).all() or not np.isfinite(norms).all() or np.any(norms == 0):
        raise ValueError("skill embeddings must contain finite, non-zero vectors")
    return matrix / norms[:, np.newaxis]


def _build_cache(fingerprint: str, skills: tuple[_SkillSnapshot, ...]) -> _SkillVectorCache:
    from services.memory_indexer import embedder

    texts = [skill.corpus_text for skill in skills]
    batches = [
        embedder.embed_documents(texts[start : start + _EMBED_BATCH_SIZE])
        for start in range(0, len(texts), _EMBED_BATCH_SIZE)
    ]
    vectors = np.concatenate(batches)
    return _SkillVectorCache(
        fingerprint=fingerprint,
        skills=skills,
        vectors=_normalized_vectors(vectors, len(skills)),
    )


def _write_cache(path: Path, cache: _SkillVectorCache) -> None:
    buffer = io.BytesIO()
    metadata = json.dumps(
        [asdict(skill) for skill in cache.skills],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    np.savez_compressed(
        buffer,
        fingerprint=np.array(cache.fingerprint),
        metadata=np.array(metadata),
        vectors=cache.vectors,
    )
    write_private_bytes(path, buffer.getvalue())


def _load_cache(
    path: Path,
    fingerprint: str,
    skills: tuple[_SkillSnapshot, ...],
) -> _SkillVectorCache | None:
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as stored:
            stored_fingerprint = str(stored["fingerprint"].item())
            stored_metadata = json.loads(str(stored["metadata"].item()))
            vectors = _normalized_vectors(stored["vectors"], len(skills))
        expected_metadata = [asdict(skill) for skill in skills]
        if stored_fingerprint != fingerprint or stored_metadata != expected_metadata:
            return None
        return _SkillVectorCache(fingerprint=fingerprint, skills=skills, vectors=vectors)
    except Exception as exc:
        logger.debug("[skill-match] cache read failed for {}: {!r}", path, exc)
        return None


def _rebuild_worker(
    key: _CacheKey,
    path: Path,
    fingerprint: str,
    skills: tuple[_SkillSnapshot, ...],
) -> None:
    try:
        cache = _build_cache(fingerprint, skills)
        _write_cache(path, cache)
        with _rebuild_lock:
            _memory_caches[key] = cache
    except Exception as exc:
        logger.warning("[skill-match] background cache rebuild failed: {!r}", exc)
    finally:
        with _rebuild_lock:
            if _rebuild_threads.get(key) is threading.current_thread():
                _rebuild_threads.pop(key)


def _schedule_rebuild(
    key: _CacheKey,
    path: Path,
    fingerprint: str,
    skills: tuple[_SkillSnapshot, ...],
) -> None:
    with _rebuild_lock:
        existing = _rebuild_threads.get(key)
        if existing is not None and existing.is_alive():
            return
        thread = threading.Thread(
            target=_rebuild_worker,
            args=(key, path, fingerprint, skills),
            name=f"skill-match-cache-{fingerprint[:12]}",
            daemon=True,
        )
        _rebuild_threads[key] = thread
        thread.start()


def _available_cache(skills: tuple[_SkillSnapshot, ...]) -> _SkillVectorCache | None:
    fingerprint = _fingerprint(skills)
    path = _cache_path(fingerprint)
    key = (path, fingerprint)
    with _rebuild_lock:
        cached = _memory_caches.get(key)
    if cached is not None:
        return cached
    cached = _load_cache(path, fingerprint, skills)
    if cached is not None:
        with _rebuild_lock:
            _memory_caches[key] = cached
        return cached
    _schedule_rebuild(key, path, fingerprint, skills)
    return None


def _top_matches(
    cache: _SkillVectorCache,
    query_vector: np.ndarray,
) -> list[_SkillSnapshot]:
    query = np.asarray(query_vector, dtype=np.float32)
    if query.ndim != 1 or query.shape[0] != cache.vectors.shape[1]:
        raise ValueError(
            f"skill query shape {query.shape} does not match cached vectors {cache.vectors.shape}"
        )
    norm = float(np.linalg.norm(query))
    if not np.isfinite(query).all() or not np.isfinite(norm) or norm == 0:
        raise ValueError("skill query embedding must be a finite, non-zero vector")
    scores = cache.vectors @ (query / norm)
    ranked = np.argsort(-scores, kind="stable")
    matches: list[_SkillSnapshot] = []
    for index in ranked:
        if scores[index] < turn_settings.agent.skill_match_min_score:
            break
        skill = cache.skills[int(index)]
        if not is_flagged(skill.description):
            matches.append(skill)
        if len(matches) == turn_settings.agent.skill_match_top_k:
            break
    return matches


def _hint_message(matches: list[_SkillSnapshot]) -> HumanMessage:
    summaries = "; ".join(
        f"`ava.skills.{skill.identifier}` — {skill.description}" for skill in matches
    )
    loads = "; ".join(f"`ava.help(ava.skills.{skill.target})`" for skill in matches)
    return system_note_message(
        content=f"Skill match: {summaries}\nLoad before using: {loads}.",
        tag=NoteTag.SDK_HINT,
    )


async def skill_match_hint(raw_text: str) -> HumanMessage | None:
    """Return a separate skill-loading hint for one expanded chat inbound."""
    if not turn_settings.agent.skill_match_enabled:
        return None
    try:
        skills = _safe_skill_snapshot()
        if not skills:
            return None
        cache = _available_cache(skills)
    except Exception as exc:
        logger.warning("[skill-match] skill scan/cache lookup failed open: {!r}", exc)
        return None
    if cache is None:
        return None

    from services.memory_indexer import embedder

    try:
        query_vector = await asyncio.wait_for(
            embedder.embed_query_async(raw_text),
            timeout=turn_settings.agent.skill_match_budget_ms / 1000,
        )
        matches = _top_matches(cache, query_vector)
    except TimeoutError:
        logger.debug(
            "[skill-match] query embedding exceeded {} ms; skipping hint",
            turn_settings.agent.skill_match_budget_ms,
        )
        return None
    except embedder.EmbeddingAPIError as exc:
        logger.warning("[skill-match] query embedding failed open: {!r}", exc)
        return None
    except Exception as exc:
        logger.warning("[skill-match] matching failed open: {!r}", exc)
        return None
    return _hint_message(matches) if matches else None
