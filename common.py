"""
Shared helpers for the gemini-to-knowledge-graph pipeline (extract / classify / vault stages).

Centralizing this avoids the config-loading logic drifting between the three
scripts, and gives every script the same friendly, fix-it-oriented error
messages instead of a raw Python traceback when something's missing.
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

PATH_KEYS = [
    "chats_dir",
    "topics_file",
    "classifications_db",
    "vault_dir",
    "prompt_file",
    "extraction_state_file",
]
API_KEYS = [
    "url",
    "temperature",
    "max_output_tokens",
    "timeout",
    "context_window_tokens",
    "safety_margin_tokens",
]
LIMIT_KEYS = ["max_retries", "max_turn_chars", "truncate_head_fraction"]
NODE_SIZING_KEYS = [
    "node_sizing.conversation",
    "node_sizing.topic.floor",
    "node_sizing.topic.ceiling",
    "node_sizing.category.floor",
    "node_sizing.category.ceiling",
]


def die(message: str):
    """Print a user-facing fix-it message and exit — never a raw traceback
    for config problems, which are the #1 first-run stumbling block."""
    print("\n" + message.strip() + "\n")
    sys.exit(1)


def load_config(require_api=False, require_limits=False, require_vault=False):
    """Load config/config.json, validate required keys, resolve relative paths.

    require_api / require_limits: pass True only from scripts that actually
    read those sections, so e.g. the extractor doesn't demand an `api` block
    it never touches.

    require_vault: validate vault-specific config (vault_dir, topics_file).
    Pass True from obsidian_layout.py which builds the Obsidian vault.
    """
    cfg_path = REPO_ROOT / "config" / "config.json"
    example_path = REPO_ROOT / "config" / "config.example.json"

    if not cfg_path.exists():
        hint = (
            "cp config.example.json config.json"
            if example_path.exists()
            else "create config/config.json — see README.md for the expected format"
        )
        die(f"""✗ Missing config file: {cfg_path}

  Fix:
    cd config && {hint}

  Then open config/config.json and check:
    - api.url / api.model         → your local (or cloud) LLM endpoint
    - api.context_window_tokens   → your model's REAL context size (important —
                                     see README "Configuring your LLM")
    - paths.*                     → fine to leave as the defaults""")

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        die(
            f"✗ config/config.json is not valid JSON: {e}\n\n"
            f"  Fix the syntax (often a trailing comma or missing quote) and re-run."
        )

    missing_paths = [k for k in PATH_KEYS if k not in cfg.get("paths", {})]
    if missing_paths:
        die(
            f"✗ config/config.json is missing paths.{{{', '.join(missing_paths)}}}\n\n"
            f"  Compare against config/config.example.json and add the missing key(s)."
        )

    if require_api:
        missing_api = [k for k in API_KEYS if k not in cfg.get("api", {})]
        if missing_api:
            die(
                f"✗ config/config.json is missing api.{{{', '.join(missing_api)}}}\n\n"
                f"  These control your LLM connection — see config/config.example.json.\n"
                f"  api.context_window_tokens especially: set it to your actual model's\n"
                f"  context length, or chats get needlessly truncated (too low) or the\n"
                f"  request can overflow your model's real limit (too high)."
            )

    if require_limits:
        missing_limits = [k for k in LIMIT_KEYS if k not in cfg.get("limits", {})]
        if missing_limits:
            die(
                f"✗ config/config.json is missing limits.{{{', '.join(missing_limits)}}}"
            )

    if require_vault:
        missing_ns = []
        for dotted_key in NODE_SIZING_KEYS:
            parts = dotted_key.split(".")
            parent = cfg
            try:
                for p in parts:
                    parent = parent[p]
            except (KeyError, TypeError):
                missing_ns.append(dotted_key)
        if missing_ns:
            die(
                f"✗ config/config.json is missing or has malformed "
                f"{' / '.join(missing_ns)}\n\n"
                f"  These control graph-view node sizing. See "
                f"config/config.example.json for the expected structure.\n"
                f"  Example:\n"
                f'    "node_sizing": {{\n'
                f'      "conversation": 8,\n'
                f'      "topic": {{ "floor": 25, "ceiling": 60 }},\n'
                f'      "category": {{ "floor": 72, "ceiling": 100 }}\n'
                f"    }}"
            )

    # Resolve relative paths to absolute
    for key in PATH_KEYS:
        raw = cfg["paths"][key]
        if raw:
            cfg["paths"][key] = str((REPO_ROOT / raw).resolve())

    cfg["_repo_root"] = str(REPO_ROOT)
    return cfg


def load_topics(cfg):
    """Load categories + topic/category mappings from config/topics.json.

    Returns:
        categories: list of valid category names
        topics_by_category: {category: [topic, ...]}
        topic_to_category: {lowercased topic name: category}
        topic_canonical_case: {lowercased topic name: canonical-cased seed name}
    """
    topics_file = cfg["paths"]["topics_file"]
    if not os.path.exists(topics_file):
        die(
            f"✗ Missing taxonomy file: {topics_file}\n\n"
            f"  Copy the template:\n"
            f"    cp config/topics.example.json config/topics.json\n"
            f"  or recreate it — see the schema in README.md."
        )

    with open(topics_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    for required in ("categories", "topics"):
        if required not in data:
            die(f"✗ config/topics.json is missing top-level key '{required}'.")

    categories = data["categories"]
    topics_by_category = {c: [] for c in categories}
    topic_to_category = {}
    topic_canonical_case = {}
    for item in data["topics"]:
        topics_by_category.setdefault(item["category"], []).append(item["name"])
        topic_to_category[item["name"].lower()] = item["category"]
        topic_canonical_case[item["name"].lower()] = item["name"]

    bad = [c for c in categories if "," in c]
    bad += [n for names in topics_by_category.values() for n in names if "," in n]
    if bad:
        die(
            f"✗ {topics_file} has category/topic names containing commas, which breaks "
            f"comma-separated parsing: {bad}\n\n  Rename them (e.g. use '&' instead of ',')."
        )

    return categories, topics_by_category, topic_to_category, topic_canonical_case


def canonicalize_topic(name, topic_canonical_case):
    """Fold case/whitespace variants of a known topic back to one canonical
    spelling, so 'prompt engineering' and 'Prompt Engineering' land on the
    same Obsidian topic note instead of silently splitting into two."""
    key = name.strip().lower()
    return topic_canonical_case.get(key, name.strip())


def dedup_case_insensitive(items: list[str]) -> list[str]:
    """Deduplicate a list of strings preserving order, case-insensitively.
    The first occurrence's casing is kept; subsequent case-folded matches are
    dropped.  Shared helper used by classify_chats.py and obsidian_layout.py."""
    seen: set[str] = set()
    return [item for item in items if not (item.lower() in seen or seen.add(item.lower()))]


def iter_chats(chats_dir: str):
    """Yield ``(fpath, chat_dict)`` for every ``.json`` file in *chats_dir*.

    Silently skips non-.json files and files that can't be parsed as JSON.
    Files are yielded in sorted (case-sensitive) name order for deterministic
    processing order across all pipeline stages.

    Shared helper that replaces ad-hoc ``os.listdir`` + ``json.load`` loops
    in ``classify_chats.py``, ``obsidian_layout.py``, and ``common.py`` itself.
    """
    if not os.path.isdir(chats_dir):
        return
    for fname in sorted(os.listdir(chats_dir)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(chats_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                yield fpath, json.load(f)
        except Exception:
            log(f"  \u26a0 Skipping unreadable {fpath}")
            continue


def load_existing_vault_state(convos_dir):
    """Scan existing conversation notes in the vault for resume capability.

    Returns: {conversation_id: (notename, note_signature_or_None)}
    """
    state = {}
    if not os.path.isdir(convos_dir):
        return state
    for fname in os.listdir(convos_dir):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(convos_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r"^---\s*\n(.*?)\n---\s*\n## Topics", content, re.DOTALL)
            if not m:
                continue
            yaml_block = m.group(1)
            cid_match = re.search(
                r'^conversation_id:\s*"(.+)"\s*$', yaml_block, re.MULTILINE
            )
            if not cid_match:
                continue
            cid = cid_match.group(1)
            hash_match = re.search(
                r'^note_signature:\s*"(.+)"\s*$', yaml_block, re.MULTILINE
            )
            content_hash = hash_match.group(1) if hash_match else None
            state[cid] = (fname[:-3], content_hash)
        except Exception:
            continue
    return state


def chat_fingerprint(chat: dict) -> str:
    """Deterministic content fingerprint shared by classify_chats.py and
    obsidian_layout.py so they never disagree on what counts as a change."""
    h = hashlib.sha256()
    h.update((chat.get("title") or "").encode("utf-8"))
    for turn in chat.get("turns", []):
        h.update((turn.get("role") or "").encode("utf-8"))
        h.update((turn.get("text") or "").encode("utf-8"))
    return h.hexdigest()[:16]


def yaml_str(value) -> str:
    """Safely quote a string for a single-line YAML frontmatter field.
    Chat titles are arbitrary user/model text and can contain quotes,
    backslashes, or newlines — naive f-string interpolation into
    `field: "{value}"` will silently corrupt the frontmatter block."""
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", " ").replace("\r", " ")
    return f'"{s}"'


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ── SQLite helpers ──────────────────────────────────────────────────────────


def get_db_connection(cfg):
    """Open (and lazily create) the classifications SQLite DB."""
    db_path = cfg["paths"]["classifications_db"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS classifications (
            conversation_id TEXT PRIMARY KEY,
            title           TEXT,
            category        TEXT,
            topic           TEXT,
            summary         TEXT,
            content_hash    TEXT,
            status          TEXT,
            error           TEXT,
            classified_at   TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON classifications(status)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ignored_conversations (
            conversation_id TEXT PRIMARY KEY,
            reason          TEXT,
            ignored_at      TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            conversation_id TEXT PRIMARY KEY,
            source          TEXT NOT NULL,
            title           TEXT,
            content_hash    TEXT NOT NULL,
            source_file     TEXT NOT NULL DEFAULT '',
            file_mtime      REAL NOT NULL DEFAULT 0,
            created_at      TEXT,
            updated_at      TEXT,
            first_seen_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            chat_type       TEXT NOT NULL DEFAULT 'regular'
        )
    """)
    conn.commit()


def upsert_classification(conn, record: dict):
    """Insert or fully replace a classification record by conversation_id.
    Unlike JSONL append, this never accumulates duplicate history for a cid."""
    conn.execute("""
        INSERT INTO classifications
            (conversation_id, title, category, topic, summary,
             content_hash, status, error, classified_at)
        VALUES (:conversation_id, :title, :category, :topic, :summary,
                :content_hash, :status, :error, :classified_at)
        ON CONFLICT(conversation_id) DO UPDATE SET
            title=excluded.title, category=excluded.category,
            topic=excluded.topic, summary=excluded.summary,
            content_hash=excluded.content_hash, status=excluded.status,
            error=excluded.error, classified_at=excluded.classified_at
    """, {
        "conversation_id": record["conversation_id"],
        "title": record.get("title", ""),
        "category": json.dumps(record.get("category", [])),
        "topic": json.dumps(record.get("topic", [])),
        "summary": record.get("summary", ""),
        "content_hash": record.get("content_hash", ""),
        "status": record.get("status", ""),
        "error": record.get("error"),
        "classified_at": record.get("classified_at", ""),
    })
    conn.commit()


def load_all_classifications(conn) -> dict:
    """Return {conversation_id: record_dict} with category/topic decoded
    back to lists.
    """
    out = {}
    for row in conn.execute("SELECT * FROM classifications"):
        rec = dict(row)
        rec["category"] = json.loads(rec.get("category") or "[]")
        rec["topic"] = json.loads(rec.get("topic") or "[]")
        out[rec["conversation_id"]] = rec
    return out


def delete_classifications(conn, cids: list) -> int:
    """Delete by conversation_id. Returns count actually deleted."""
    if not cids:
        return 0
    placeholders = ",".join("?" * len(cids))
    cur = conn.execute(
        f"DELETE FROM classifications WHERE conversation_id IN ({placeholders})",
        cids,
    )
    conn.commit()
    return cur.rowcount


# ── Chats table helpers ─────────────────────────────────────────────────────


def upsert_chat(conn, record: dict):
    """Insert or update a chat record by conversation_id.
    first_seen_at is preserved on conflict (ON CONFLICT doesn't touch it)."""
    conn.execute("""
        INSERT INTO chats
            (conversation_id, source, title, content_hash,
             source_file, file_mtime,
             created_at, updated_at, first_seen_at, last_seen_at,
             chat_type)
        VALUES (:conversation_id, :source, :title, :content_hash,
                :source_file, :file_mtime,
                :created_at, :updated_at, :first_seen_at, :last_seen_at,
                :chat_type)
        ON CONFLICT(conversation_id) DO UPDATE SET
            source=excluded.source,
            title=excluded.title,
            content_hash=excluded.content_hash,
            source_file=excluded.source_file,
            file_mtime=excluded.file_mtime,
            created_at=excluded.created_at,
            updated_at=excluded.updated_at,
            last_seen_at=excluded.last_seen_at,
            chat_type=excluded.chat_type
    """, {
        "conversation_id": record["conversation_id"],
        "source": record.get("source", ""),
        "title": record.get("title"),
        "content_hash": record.get("content_hash", ""),
        "source_file": record.get("source_file", ""),
        "file_mtime": record.get("file_mtime", 0.0),
        "chat_type": record.get("chat_type", "regular"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "first_seen_at": record.get("first_seen_at", ""),
        "last_seen_at": record.get("last_seen_at", ""),
    })
    conn.commit()


def get_chat_row(conn, conversation_id) -> dict | None:
    """Return the full row for a conversation_id, or None if absent."""
    row = conn.execute(
        "SELECT * FROM chats WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    return dict(row) if row else None


def get_chat_rows(conn, conversation_ids: list) -> dict:
    """Return {conversation_id: row_dict} for all matching IDs.
    Missing IDs are omitted from the result."""
    if not conversation_ids:
        return {}
    placeholders = ",".join("?" * len(conversation_ids))
    result = {}
    for row in conn.execute(
        f"SELECT * FROM chats WHERE conversation_id IN ({placeholders})",
        conversation_ids,
    ):
        result[row["conversation_id"]] = dict(row)
    return result


def sync_chats_to_db(conn, chats_dir):
    """Scan chats_dir and upsert every chat file into the ``chats`` table.

    Purely additive — never deletes. Safe to call on every run.
    Uses ``os.scandir`` + ``file_mtime`` pre-filtering: if a file's mtime
    matches the stored value for that source_file, the file is skipped
    without reading or hashing it.  Only files whose mtime has changed
    (or that are new) are opened and fingerprinted.
    """
    if not os.path.isdir(chats_dir):
        return
    now = datetime.now(timezone.utc).isoformat()

    # Build a lookup: source_file -> (content_hash, file_mtime) for known rows
    known = {}
    for row in conn.execute(
        "SELECT conversation_id, source_file, content_hash, file_mtime FROM chats"
    ):
        known[row["source_file"]] = (row["conversation_id"], row["content_hash"], row["file_mtime"])

    for entry in os.scandir(chats_dir):
        if not entry.name.endswith(".json") or not entry.is_file():
            continue
        fname = entry.name
        fpath = entry.path
        mtime = entry.stat().st_mtime

        # Pre-filter: if source_file + file_mtime match, skip entirely
        known_row = known.get(fname)
        if known_row and known_row[2] == mtime:
            continue

        # Read + fingerprint
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                chat = json.load(f)
        except Exception:
            continue
        cid = chat.get("conversation_id")
        if not cid:
            continue
        fp = chat_fingerprint(chat)
        existing = get_chat_row(conn, cid)
        if existing and existing["content_hash"] == fp:
            if existing["source_file"] != fname:
                log(f"  ⚠ Chat file renamed: '{existing['source_file']}' -> '{fname}'"
                    f" (conversation_id unchanged). The old filename is still stored"
                    f" in the database — renaming chat files is not supported and"
                    f" may cause issues.")
            continue
        upsert_chat(conn, {
            "conversation_id": cid,
            "source": chat.get("source", ""),
            "title": chat.get("title"),
            "content_hash": fp,
            "source_file": fname,
            "file_mtime": mtime,
            "created_at": chat.get("created_at"),
            "updated_at": chat.get("updated_at"),
            "first_seen_at": now,
            "last_seen_at": now,
        })


# ── Ignored conversations (replaces the old ignore_conversation in JSON) ────


def add_ignored_conversations(conn, cids: list, reason: str = "pruned-orphan"):
    """Idempotent — re-ignoring an already-ignored cid just refreshes
    reason/timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """INSERT INTO ignored_conversations (conversation_id, reason, ignored_at)
           VALUES (?, ?, ?)
           ON CONFLICT(conversation_id) DO UPDATE SET
               reason=excluded.reason, ignored_at=excluded.ignored_at""",
        [(cid, reason, now) for cid in cids],
    )
    conn.commit()


def load_ignored_conversations(conn) -> set:
    """Return set of all ignored conversation IDs."""
    return {
        row["conversation_id"]
        for row in conn.execute("SELECT conversation_id FROM ignored_conversations")
    }


def remove_ignored_conversations(conn, cids: list) -> int:
    """Remove cids from the ignore list (for a future un-ignore flag)."""
    if not cids:
        return 0
    placeholders = ",".join("?" * len(cids))
    cur = conn.execute(
        f"DELETE FROM ignored_conversations WHERE conversation_id IN ({placeholders})",
        cids,
    )
    conn.commit()
    return cur.rowcount


# ── Prune helpers (shared by classify_chats.py and obsidian_layout.py) ──────


def find_orphaned_cids(known_cids: set, chats_dir: str) -> set:
    """Return cids present in known_cids but with no matching chat file in
    chats_dir. Matches by reading each file's conversation_id field, not by
    filename — a file may have been renamed or sanitized differently."""
    if not os.path.isdir(chats_dir):
        return set()
    existing_cids = set()
    for fpath, chat in iter_chats(chats_dir):
        cid = chat.get("conversation_id")
        if cid:
            existing_cids.add(cid)
    return known_cids - existing_cids


def exceeds_prune_safety_threshold(orphan_count: int, known_count: int, max_ratio: float = 0.3) -> bool:
    """Return True if the proportion of orphaned records exceeds the safety
    threshold, indicating the user may be pointing at the wrong chats_dir."""
    if known_count <= 0:
        return False
    return (orphan_count / known_count) > max_ratio






