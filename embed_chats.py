"""
Embedding stage — embeds each classified chat's title+summary using a
local or cloud embedding endpoint, and precomputes top-K similarity links
for the Similarity Vault (embedding_layout.py).

Usage:
    python embed_chats.py           # embed everything stale/missing, rebuild links
    python embed_chats.py 20        # process only the next 20 (testing)
    python embed_chats.py --recompute-links   # skip the API, just rebuild links
"""

import hashlib
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import requests
from dotenv import load_dotenv

from common import (
    get_db_connection,
    load_all_classifications,
    load_all_embeddings,
    load_all_similarity_links,
    load_config,
    load_embedding_config,
    log,
    pack_vector,
    replace_all_similarity_links,
    truncate_title,
    unpack_vector,
    upsert_embedding,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def load_env():
    """Load .env and return LLM_API_KEY (may be empty for local)."""
    load_dotenv()
    return os.getenv("LLM_API_KEY", "")


def strip_summary_prefixes(text: str, prefixes: list[str]) -> str:
    """Remove one configured leading boilerplate phrase (e.g. 'This
    conversation', 'The user') from the start of embedding input text,
    keeping whatever follows (typically the verb — 'discusses',
    'requested' — which still carries useful signal). Longest configured
    prefix is checked first. Only matches at the very start of the
    string, never mid-sentence."""
    stripped = text.lstrip()
    for prefix in sorted(prefixes, key=len, reverse=True):
        if stripped.lower().startswith(prefix.lower()):
            stripped = stripped[len(prefix):].lstrip(" ,:;-")
            break
    return stripped


def compute_summary_signature(title: str, summary: str, model: str) -> str:
    """Deterministic staleness signature for a chat's embedding input.

    sha256 of title|summary|model, truncated to 16 hex chars (64 bits) —
    same convention as chat_fingerprint() in common.py. 64 bits is far
    more than enough for change detection at this scale; collisions are
    negligible.

    The title is truncated with truncate_title() before hashing so the
    signature matches exactly what gets embedded (embed_chats.py embeds
    truncate_title(title) + stripped summary). For titles <= 120 chars
    truncate_title() is a no-op, so existing stored hashes stay valid."""
    h = hashlib.sha256()
    h.update(truncate_title(title).encode("utf-8"))
    h.update(b"|")
    h.update((summary or "").encode("utf-8"))
    h.update(b"|")
    h.update(model.encode("utf-8"))
    return h.hexdigest()[:16]


def compute_todo(classifications: dict, existing_embeddings: dict, current_model: str):
    """Returns [(cid, title, summary, signature), ...] for chats that need
    (re)embedding: status == 'ok', non-empty summary, and either no
    existing embedding row or a signature mismatch (text or model changed)."""
    todo = []
    for cid, rec in classifications.items():
        if rec.get("status") != "ok":
            continue
        title = rec.get("title") or ""
        summary = rec.get("summary") or ""
        if not summary.strip():
            continue
        sig = compute_summary_signature(title, summary, current_model)
        existing = existing_embeddings.get(cid)
        if existing and existing.get("summary_hash") == sig:
            continue
        todo.append((cid, title, summary, sig))
    return todo


def _api_headers(api_key: str) -> dict:
    """Build the JSON headers for embedding API calls."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def embed_batch(texts: list[str], api_cfg: dict, api_key: str):
    """POST a batch of texts to the OpenAI-compatible /v1/embeddings
    endpoint. Returns a list of (vector, dim) tuples in the same order
    as *texts*. Retries with backoff on connection/HTTP errors."""
    headers = _api_headers(api_key)

    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(
                api_cfg["url"],
                headers=headers,
                json={"model": api_cfg["model"], "input": texts},
                timeout=api_cfg["timeout"],
            )
            r.raise_for_status()
            data = r.json()["data"]
            data.sort(key=lambda d: d.get("index", 0))  # defensive — don't trust order
            return [(d["embedding"], len(d["embedding"])) for d in data]
        except KeyboardInterrupt:
            raise  # never swallow Ctrl-C into a retry loop
        except Exception as e:
            last_err = e
            log(f"    ⚠ Embedding API error, retry {attempt+1}/3: {e}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Embedding API unreachable after 3 attempts: {last_err}")


def compute_top_k_links(vectors_by_cid: dict, top_k: int, min_similarity: float):
    """vectors_by_cid: {cid: np.ndarray} — already-unpacked vectors for
    the CURRENT model only. Returns rows ready for
    replace_all_similarity_links()."""
    cids = list(vectors_by_cid.keys())
    if len(cids) < 2:
        return []

    matrix = np.stack([vectors_by_cid[cid] for cid in cids]).astype(np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1e-8  # guard against a zero vector
    normalized = matrix / norms

    sim = normalized @ normalized.T
    np.fill_diagonal(sim, -1.0)  # exclude self-matches

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for i, cid in enumerate(cids):
        row_scores = sim[i]
        # argpartition is O(n) per row vs argsort's O(n log n); only the
        # top_k indices are needed. kth must be < len(row_scores), so cap
        # it when top_k >= n. The partition is unordered, so sort just the
        # selected slice to keep ranks in descending order.
        kth = min(top_k, len(row_scores) - 1)
        top_idx = np.argpartition(-row_scores, kth)[:top_k]
        top_idx = top_idx[np.argsort(-row_scores[top_idx])]
        rank = 1
        for j in top_idx:
            score = float(row_scores[j])
            if score < min_similarity:
                break  # sorted descending — safe to stop early
            rows.append({
                "conversation_id": cid,
                "neighbor_id": cids[j],
                "score": score,
                "rank": rank,
                "computed_at": now,
            })
            rank += 1
    return rows


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    cfg = load_config()
    embed_cfg = load_embedding_config()
    api_key = load_env()
    api_cfg = embed_cfg["api"]

    # CLI
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = {a for a in sys.argv[1:] if a.startswith("-")}
    recompute_links = "--recompute-links" in flags
    try:
        limit = int(args[0]) if args else None
    except ValueError:
        log(f"  ✗ Invalid limit argument: {args[0]!r} — must be a number")
        return

    log("=" * 60)
    log(f"Embedding stage ({api_cfg['model']})")
    if recompute_links:
        log("  Mode: RECOMPUTE LINKS ONLY (no API calls)")
    log("=" * 60)

    # API connectivity test (skipped in --recompute-links mode)
    if not recompute_links:
        log("Testing API connectivity...")
        test_headers = _api_headers(api_key)
        try:
            resp = requests.post(
                api_cfg["url"],
                headers=test_headers,
                json={"model": api_cfg["model"], "input": ["ping"]},
                timeout=10,
            )
            if resp.status_code != 200:
                log(f"  ✗ API returned status {resp.status_code} — check api.url in config/embedding.json")
                return
            log("  ✓ API reachable")
        except Exception as e:
            log(f"  ✗ API not reachable: {e}")
            return

    conn = get_db_connection(cfg)
    try:
        # ── Pass 1: embed stale/missing chats ──────────────────────────────
        embedded_this_run = 0
        errors_this_run = 0
        if not recompute_links:
            classifications = load_all_classifications(conn)
            existing = load_all_embeddings(conn)
            todo = compute_todo(classifications, existing, api_cfg["model"])
            if limit:
                todo = todo[:limit]
            log(f"{len(todo)} chats to embed\n")
            if todo:
                batch_size = api_cfg["batch_size"]
                prefixes = embed_cfg["text_prep"].get("strip_prefixes", [])
                for i in range(0, len(todo), batch_size):
                    batch = todo[i:i + batch_size]
                    texts = []
                    for _cid, title, summary, _sig in batch:
                        stripped_summary = strip_summary_prefixes(summary, prefixes)
                        embed_title = truncate_title(title)
                        if embed_title != title:
                            log(f"    ⚠ title truncated for {_cid} "
                                f"({len(title)} → {len(embed_title)} chars)")
                        texts.append(f"{embed_title} {stripped_summary}".strip())
                    log(f"[{i+1}-{i+len(batch)}/{len(todo)}] embedding batch of {len(batch)}")
                    try:
                        results = embed_batch(texts, api_cfg, api_key)
                    except Exception as e:
                        log(f"  ✗ Batch failed after retries: {e}")
                        errors_this_run += len(batch)
                        continue
                    now = datetime.now(timezone.utc).isoformat()
                    for (cid, title, summary, sig), (vector, dim) in zip(batch, results):
                        upsert_embedding(conn, {
                            "conversation_id": cid,
                            "summary_hash": sig,
                            "model": api_cfg["model"],
                            "dim": dim,
                            "vector": pack_vector(vector),
                            "embedded_at": now,
                        })
                        embedded_this_run += 1
                    log(f"    ✓ {len(batch)} embedded")
        else:
            log("Skipping pass 1 (--recompute-links)")

        # ── Pass 2: rebuild similarity_links from current-model embeddings ─
        all_embeddings = load_all_embeddings(conn)
        current_model = api_cfg["model"]
        vectors_by_cid = {}
        for cid, rec in all_embeddings.items():
            if rec.get("model") != current_model:
                continue
            try:
                vectors_by_cid[cid] = unpack_vector(rec["vector"], rec["dim"])
            except ValueError as e:
                log(f"  ⚠ Skipping {cid}: {e}")

        if len(vectors_by_cid) < 2:
            log("No current-model embeddings found — similarity_links cleared.")
            replace_all_similarity_links(conn, [])
        else:
            rows = compute_top_k_links(vectors_by_cid, embed_cfg["top_k"], embed_cfg["min_similarity"])
            replace_all_similarity_links(conn, rows)
            log(f"Rebuilt similarity_links: {len(rows)} links across {len(vectors_by_cid)} chats")

        # ── Final summary ──────────────────────────────────────────────────
        links = load_all_similarity_links(conn)
        total_links = sum(len(lst) for lst in links.values())
        # chats with embeddings but no links at all (below threshold)
        embedded_cids = {cid for cid, rec in all_embeddings.items() if rec.get("model") == current_model}
        zero_link_chats = [cid for cid in embedded_cids if not links.get(cid)]

        log(f"\n{'='*60}")
        log(f"DONE! {embedded_this_run} embedded, {errors_this_run} batch errors")
        log(f"Total chats with embeddings (current model): {len(embedded_cids)}")
        log(f"Total similarity links: {total_links}")
        if zero_link_chats:
            log(f"⚠ {len(zero_link_chats)} chats have no links above min_similarity "
                f"({embed_cfg['min_similarity']})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()