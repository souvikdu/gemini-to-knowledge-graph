"""
Standalone review script — generates, refreshes, and manages the local
review manifest (review/chats_to_review.csv).

Usage:
    python review_chats.py                  # Refresh manifest (auto-scan new/changed)
    python review_chats.py --scan-sensitive # Force full sensitive scan on all chats (required after updating sensitive_patterns.json)
    python review_chats.py --mark-reviewed   # Confirm and mark all unreviewed (NO or RE-REVIEW) as YES
    python review_chats.py --mark-reviewed -y  # Same, skip confirmation (useful for scripting)
    python review_chats.py --show-sensitive <conversation_id>  # Show sensitive matches for one chat
    python review_chats.py --show-sensitive-all               # Show sensitive matches for all flagged chats
    python review_chats.py --mask-sensitive                   # Preview which reviewed KEEP chats would be masked
    python review_chats.py --mask-sensitive --apply            # Redact sensitive spans in reviewed KEEP chats
"""

import csv
import json
import os
import re
import sys
from datetime import datetime

from common import (
    REPO_ROOT,
    chat_fingerprint,
    die,
    get_db_connection,
    load_config,
    log,
    sync_chats_to_db,
    truncate_title,
)

MANIFEST_HEADER = [
    "updated_at",
    "source",
    "conversation_id",
    "content_hash",
    "reviewed",
    "action",
    "title",
    "flags",
]
MANIFEST_DIR = os.path.join(REPO_ROOT, "review")
MANIFEST_PATH = os.path.join(MANIFEST_DIR, "chats_to_review.csv")
SENSITIVE_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "sensitive_patterns.json")


def load_sensitive_rules(config_path: str = SENSITIVE_CONFIG_PATH):
    """Load sensitive scan patterns and keywords from JSON config file.

    Returns (patterns_dict, keywords_dict) where:
      - patterns_dict: {alias: compiled_regex}
      - keywords_dict: {alias: tuple_of_literal_strings}
    Returns (None, None) if config file does not exist.
    Dies if file exists but is malformed or regexes fail to compile.
    """
    if not os.path.exists(config_path):
        return None, None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        die(f"Error reading sensitive patterns config '{config_path}': {e}")

    if not isinstance(data, dict):
        die(f"Invalid sensitive patterns config '{config_path}': root must be a JSON object.")

    patterns_raw = data.get("patterns", {})
    keywords_raw = data.get("keywords", {})

    if not isinstance(patterns_raw, dict) or not isinstance(keywords_raw, dict):
        die(f"Invalid sensitive patterns config '{config_path}': 'patterns' and 'keywords' must be dicts.")

    patterns = {}
    for alias, pattern_str in patterns_raw.items():
        if not alias or not isinstance(alias, str) or not isinstance(pattern_str, str):
            die(f"Invalid pattern rule in '{config_path}': alias and pattern must be non-empty strings.")
        try:
            patterns[alias] = re.compile(pattern_str)
        except re.error as e:
            die(f"Invalid regex for alias '{alias}' in '{config_path}': {e}")

    keywords = {}
    for alias, kw_val in keywords_raw.items():
        if not alias or not isinstance(alias, str):
            die(f"Invalid keyword rule in '{config_path}': alias must be a non-empty string.")
        if not isinstance(kw_val, list) or not all(isinstance(item, str) and item for item in kw_val):
            die(f"Invalid keyword rule for alias '{alias}' in '{config_path}': "
                f"value must be a non-empty array of non-empty strings.")
        keywords[alias] = tuple(kw_val)

    return patterns, keywords


# ── Luhn validator for credit_card ──────────────────────────────────────────


def luhn_valid(digits: str) -> bool:
    """Validate a digit string using the Luhn algorithm (ISO/IEC 7812)."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


VALIDATORS: dict[str, callable] = {
    "credit_card": lambda matched: luhn_valid(re.sub(r"[ -]", "", matched)),
}


# ── Masking helpers ─────────────────────────────────────────────────────────


def _mask_credit_card(matched_text: str) -> str:
    digits = re.sub(r"[ -]", "", matched_text)
    return f"[REDACTED:credit_card ...{digits[-4:]}]"


def _mask_email(matched_text: str) -> str:
    _, sep, domain = matched_text.partition("@")
    # Drop the "@" prefix so the email regex can't re-match the masked text
    return f"[REDACTED:email domain={domain}]" if sep else "[REDACTED:email]"


PARTIAL_MASKERS: dict[str, callable] = {
    "credit_card": _mask_credit_card,
    "email": _mask_email,
}
# Anything not listed here (ipv4, ipv6, and every keyword alias) gets a
# full "[REDACTED:{alias}]" replacement. Keyword aliases must NEVER be
# added to PARTIAL_MASKERS — they're literal real PII the user configured,
# not a low-sensitivity value like a card's last 4 digits.


def mask_chat_content(chat: dict, patterns: dict | None, keywords: dict | None) -> tuple[dict, bool]:
    """Redact matched spans in title + turn text of *chat*.

    Mutates and returns the dict plus a bool indicating whether anything
    actually changed.  Pure aside from mutating the passed-in dict — no
    file I/O, directly unit testable.
    """
    changed = False

    def _mask_text(text):
        nonlocal changed
        if not text:
            return text
        original = text
        if patterns:
            for alias, compiled_re in patterns.items():
                validator = VALIDATORS.get(alias)

                def _sub(m, alias=alias, validator=validator):
                    matched = m.group()
                    if validator and not validator(matched):
                        return matched
                    masker = PARTIAL_MASKERS.get(alias)
                    return masker(matched) if masker else f"[REDACTED:{alias}]"

                text = compiled_re.sub(_sub, text)
        if keywords:
            for alias, kw_list in keywords.items():
                for kw in kw_list:
                    text = re.compile(re.escape(kw), re.IGNORECASE).sub(
                        f"[REDACTED:{alias}]", text
                    )
        if text != original:
            changed = True
        return text

    if chat.get("title"):
        chat["title"] = _mask_text(chat["title"])
    for turn in chat.get("turns", []):
        if turn.get("text"):
            turn["text"] = _mask_text(turn["text"])

    return chat, changed


def mask_chat_file(fpath: str, patterns, keywords) -> dict | None:
    """Read, mask, and atomically overwrite a chat JSON file.

    Returns the masked chat dict if anything was changed, or None if no
    masking was needed (e.g. the flagged span was Luhn-invalid, or already
    edited by hand).
    """
    with open(fpath, "r", encoding="utf-8") as f:
        chat = json.load(f)
    masked_chat, changed = mask_chat_content(chat, patterns, keywords)
    if not changed:
        return None
    tmp = fpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(masked_chat, f, indent=2, ensure_ascii=False)
    os.replace(tmp, fpath)
    return masked_chat


# ── Scanning ────────────────────────────────────────────────────────────────


def scan_chat_file(fpath: str, patterns: dict | None, keywords: dict | None) -> str:
    """Scan a chat JSON file against regex patterns and literal keywords.

    Returns deterministic comma-separated string of matched safe aliases.
    """
    if not patterns and not keywords:
        return ""
    if not os.path.isfile(fpath):
        return ""

    try:
        with open(fpath, "r", encoding="utf-8") as f:
            chat = json.load(f)
    except Exception:
        return ""

    matched_aliases = set()
    text_elements = []

    title = chat.get("title")
    if title:
        text_elements.append(str(title))

    for turn in chat.get("turns", []):
        text = turn.get("text")
        if text:
            text_elements.append(str(text))

    if patterns:
        for alias, compiled_re in patterns.items():
            validator = VALIDATORS.get(alias)
            for text in text_elements:
                if validator:
                    if any(validator(m.group()) for m in compiled_re.finditer(text)):
                        matched_aliases.add(alias)
                        break
                else:
                    if compiled_re.search(text):
                        matched_aliases.add(alias)
                        break

    if keywords:
        for alias, kw_list in keywords.items():
            for text in text_elements:
                text_lower = text.lower()
                if any(kw.lower() in text_lower for kw in kw_list):
                    matched_aliases.add(alias)
                    break

    return ",".join(sorted(matched_aliases))


def scan_chat_file_verbose(
    fpath: str, patterns: dict | None, keywords: dict | None
) -> dict[str, list[tuple[str, str]]]:
    """Scan a chat JSON file and return per-alias match details with context.

    Returns {alias: [(context_string, matched_text), ...]} where context_string
    includes surrounding characters (with ``...`` ellipsis if truncated).

    Unlike ``scan_chat_file()`` this captures *every* match per alias, not just
    the first, and includes source context for display.
    """
    result: dict[str, list[tuple[str, str]]] = {}
    if not patterns and not keywords:
        return result
    if not os.path.isfile(fpath):
        return result

    try:
        with open(fpath, "r", encoding="utf-8") as f:
            chat = json.load(f)
    except Exception:
        return result

    text_elements: list[str] = []
    title = chat.get("title")
    if title:
        text_elements.append(str(title))
    for turn in chat.get("turns", []):
        text = turn.get("text")
        if text:
            text_elements.append(str(text))

    ctx = 40  # context characters on each side

    if patterns:
        for alias, compiled_re in patterns.items():
            validator = VALIDATORS.get(alias)
            matches: list[tuple[str, str]] = []
            for text in text_elements:
                for m in compiled_re.finditer(text):
                    if validator and not validator(m.group()):
                        continue
                    start = max(0, m.start() - ctx)
                    end = min(len(text), m.end() + ctx)
                    prefix = "..." if start > 0 else ""
                    suffix = "..." if end < len(text) else ""
                    context = text[start:end].replace("\n", " ")
                    context_str = f"{prefix}{context}{suffix}"
                    matches.append((context_str, m.group()))
            if matches:
                result[alias] = matches

    if keywords:
        for alias, kw_list in keywords.items():
            matches = []
            for kw in kw_list:
                kw_lower = kw.lower()
                for text in text_elements:
                    text_lower = text.lower()
                    idx = 0
                    while True:
                        pos = text_lower.find(kw_lower, idx)
                        if pos == -1:
                            break
                        start = max(0, pos - ctx)
                        end = min(len(text), pos + len(kw) + ctx)
                        prefix = "..." if start > 0 else ""
                        suffix = "..." if end < len(text) else ""
                        context = text[start:end].replace("\n", " ")
                        context_str = f"{prefix}{context}{suffix}"
                        matches.append((context_str, text[pos : pos + len(kw)]))
                        idx = pos + 1
            if matches:
                result[alias] = matches

    return result


def read_and_validate_manifest(csv_path: str = MANIFEST_PATH):
    """Read existing review CSV manifest and validate structure and values.

    Returns list of row dicts if valid, or None if file does not exist.
    Raises ValueError / RuntimeError if manifest is malformed or invalid.
    """
    if not os.path.exists(csv_path):
        return None

    rows = []
    seen_cids = set()

    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("Review manifest CSV is empty.")

            header_clean = [col.strip() for col in header]
            if header_clean != MANIFEST_HEADER:
                raise ValueError(
                    f"Review manifest header mismatch. Expected {MANIFEST_HEADER}, got {header_clean}"
                )

            for line_idx, line in enumerate(reader, start=2):
                if not line or not any(field.strip() for field in line):
                    continue
                if len(line) != len(MANIFEST_HEADER):
                    raise ValueError(
                        f"Line {line_idx} in manifest has {len(line)} columns, expected {len(MANIFEST_HEADER)}."
                    )

                row = dict(zip(MANIFEST_HEADER, line))
                cid = row["conversation_id"].strip()
                if not cid:
                    raise ValueError(f"Line {line_idx} in manifest has empty conversation_id.")
                if cid in seen_cids:
                    raise ValueError(f"Duplicate conversation_id '{cid}' found in manifest (line {line_idx}).")
                seen_cids.add(cid)

                action = row["action"].strip().upper()
                if action not in ("KEEP", "DEL"):
                    raise ValueError(
                        f"Invalid action '{row['action']}' for conversation_id '{cid}'. Expected KEEP or DEL."
                    )

                reviewed = row["reviewed"].strip().upper()
                if reviewed not in ("YES", "NO", "RE-REVIEW"):
                    raise ValueError(
                        f"Invalid reviewed state '{row['reviewed']}' for conversation_id '{cid}'. Expected YES, NO, or RE-REVIEW."
                    )

                row["conversation_id"] = cid
                row["action"] = action
                row["reviewed"] = reviewed
                row["updated_at"] = row["updated_at"].strip()
                row["source"] = row["source"].strip()
                row["content_hash"] = row["content_hash"].strip()
                rows.append(row)

    except Exception as e:
        raise RuntimeError(f"Failed to parse review manifest '{csv_path}': {e}") from e

    return rows


def _truncate_to_minute(ts: str | None) -> str:
    """Truncate an ISO-8601 timestamp to minute precision for display.

    ``2026-07-19T15:23:11.389619+00:00`` → ``2026-07-19T15:23``

    Returns ``""`` if *ts* is ``None`` or doesn't match the expected pattern.
    """
    if not ts:
        return ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})", ts)
    return m.group(1) if m else ts


def write_manifest_atomically(rows: list, csv_path: str = MANIFEST_PATH):
    """Write list of row dicts to CSV manifest atomically via a temp file.

    ``updated_at`` is truncated to minute precision for display; the
    internal sort (full-precision ISO 8601) is unaffected because callers
    sort before passing *rows* here.
    """
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    tmp_path = csv_path + ".tmp"

    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(MANIFEST_HEADER)
        for r in rows:
            row_out = dict(r)
            row_out["updated_at"] = _truncate_to_minute(row_out.get("updated_at", ""))
            writer.writerow([row_out.get(col, "") for col in MANIFEST_HEADER])

    try:
        os.replace(tmp_path, csv_path)
    except PermissionError:
        die(
            f"Could not write to '{csv_path}' — the file may be open in another "
            f"application (e.g. Excel, Google Sheets, or a text editor).\n"
            f"Please close the file and try again."
        )


def generate_or_refresh_manifest(conn, cfg: dict, scan_sensitive: bool = False):
    """Generate or refresh review manifest CSV from the database state."""
    chats_dir = cfg["paths"]["chats_dir"]
    log("Syncing chat files to DB before updating review manifest...")
    sync_chats_to_db(conn, chats_dir)

    # 1. Load sensitive scan rules
    patterns, keywords = None, None
    if os.path.exists(SENSITIVE_CONFIG_PATH):
        patterns, keywords = load_sensitive_rules(SENSITIVE_CONFIG_PATH)
    elif scan_sensitive:
        die(
            f"--scan-sensitive flag supplied, but sensitive configuration file "
            f"is missing at '{SENSITIVE_CONFIG_PATH}'."
        )

    # 2. Read existing manifest if present
    existing_rows = None
    if os.path.exists(MANIFEST_PATH):
        try:
            existing_rows = read_and_validate_manifest(MANIFEST_PATH)
        except Exception as e:
            die(f"Manifest validation failed: {e}\nFix or remove '{MANIFEST_PATH}' to continue.")

    # 3. Query DB chats
    db_rows = conn.execute(
        "SELECT conversation_id, source, title, content_hash, source_file, created_at, updated_at "
        "FROM chats ORDER BY updated_at DESC, conversation_id DESC"
    ).fetchall()

    db_chats = {r["conversation_id"]: dict(r) for r in db_rows}

    # 4. Build manifest rows
    final_rows = []

    # Track which DB chats were processed from existing manifest
    processed_cids = set()

    if existing_rows:
        for old_row in existing_rows:
            cid = old_row["conversation_id"]
            if cid not in db_chats:
                # Chat dropped from DB — omit from manifest
                continue

            processed_cids.add(cid)
            db_chat = db_chats[cid]
            new_hash = db_chat.get("content_hash", "")
            old_hash = old_row.get("content_hash", "")

            # Check content hash mismatch
            hash_changed = old_hash != new_hash

            updated_row = dict(old_row)
            updated_row["updated_at"] = db_chat.get("updated_at") or ""
            updated_row["source"] = db_chat.get("source") or old_row.get("source", "")
            updated_row["title"] = truncate_title(db_chat.get("title"))
            updated_row["content_hash"] = new_hash

            # Perform sensitive scan if needed
            old_flags = old_row.get("flags", "")
            need_scan = scan_sensitive or (hash_changed and (patterns or keywords))
            if need_scan:
                fpath = os.path.join(chats_dir, db_chat.get("source_file", ""))
                updated_row["flags"] = scan_chat_file(fpath, patterns, keywords)
            new_flags = updated_row.get("flags", "")
            flags_changed = bool(old_flags != new_flags)

            if (hash_changed or flags_changed) and old_row["reviewed"] == "YES":
                updated_row["reviewed"] = "RE-REVIEW"
            final_rows.append(updated_row)

    # Add newly discovered chats (in DB but not in existing manifest)
    for cid, db_chat in db_chats.items():
        if cid in processed_cids:
            continue

        fpath = os.path.join(chats_dir, db_chat.get("source_file", ""))
        flags = ""
        if patterns or keywords:
            flags = scan_chat_file(fpath, patterns, keywords)

        final_rows.append({
            "updated_at": db_chat.get("updated_at") or "",
            "source": db_chat.get("source", ""),
            "conversation_id": cid,
            "content_hash": db_chat.get("content_hash", ""),
            "reviewed": "NO",
            "action": "KEEP",
            "title": truncate_title(db_chat.get("title")),
            "flags": flags,
        })

    # Sort all rows by updated_at DESC, conversation_id DESC
    final_rows.sort(key=lambda r: (r["updated_at"], r["conversation_id"]), reverse=True)

    write_manifest_atomically(final_rows, MANIFEST_PATH)

    # Log summary
    no_count = sum(1 for r in final_rows if r["reviewed"] == "NO")
    re_count = sum(1 for r in final_rows if r["reviewed"] == "RE-REVIEW")
    yes_count = sum(1 for r in final_rows if r["reviewed"] == "YES")
    delete_count = sum(1 for r in final_rows if r["action"] == "DEL")
    flagged_count = sum(1 for r in final_rows if r.get("flags"))

    log(f"Updated review manifest: '{MANIFEST_PATH}'")
    log(
        f"  Total: {len(final_rows)} | New (NO): {no_count} | Changed (RE-REVIEW): {re_count} "
        f"| Reviewed (YES): {yes_count} | Marked DEL: {delete_count}"
        + (f" | Flagged: {flagged_count}" if (patterns or keywords or scan_sensitive) else "")
    )


def mark_all_reviewed(yes_flag=False):
    """Collapse all NO and RE-REVIEW states to YES.
    
    If yes_flag is True, skips the interactive confirmation prompt.
    """
    if not os.path.exists(MANIFEST_PATH):
        die(f"Review manifest missing at '{MANIFEST_PATH}'. Run 'python review_chats.py' first.")

    try:
        rows = read_and_validate_manifest(MANIFEST_PATH)
    except Exception as e:
        die(f"Manifest validation failed: {e}")

    no_count = sum(1 for r in rows if r["reviewed"] == "NO")
    re_count = sum(1 for r in rows if r["reviewed"] == "RE-REVIEW")
    yes_count = sum(1 for r in rows if r["reviewed"] == "YES")
    unreviewed_total = no_count + re_count

    if unreviewed_total == 0:
        log("No unreviewed chats (NO or RE-REVIEW) found in review manifest.")
        return

    log("Review Status Summary:")
    log(f"  - Unreviewed (NO):          {no_count}")
    log(f"  - Content changed (RE-REVIEW): {re_count}")
    log(f"  - Already reviewed (YES):   {yes_count}")
    log(f"  - Total to mark as YES:     {unreviewed_total}")

    if yes_flag:
        confirm = True
    else:
        try:
            ans = input(f"\nMark all {unreviewed_total} unreviewed chat(s) as YES? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        confirm = ans in ("y", "yes")

    if confirm:
        for r in rows:
            r["reviewed"] = "YES"
        write_manifest_atomically(rows, MANIFEST_PATH)
        log(f"Marked all {unreviewed_total} chat(s) as reviewed (YES) in '{MANIFEST_PATH}'.")
    else:
        log("Aborted. Manifest was not modified.")


def _resolve_chat_file(cid: str, conn, chats_dir: str) -> str | None:
    """Resolve and validate a chat's JSON file path from conversation_id.

    Returns the absolute file path if valid, or None if the record is
    missing, the path escapes *chats_dir*, the file isn't valid JSON, or
    the embedded ``conversation_id`` doesn't match.
    """
    row = conn.execute(
        "SELECT source_file FROM chats WHERE conversation_id = ?", (cid,)
    ).fetchone()
    if not row or not row["source_file"]:
        return None

    source_file = row["source_file"]
    abs_chats_dir = os.path.abspath(chats_dir)
    candidate_path = os.path.abspath(os.path.join(abs_chats_dir, source_file))

    # Path-traversal guard
    if not candidate_path.startswith(abs_chats_dir + os.sep) and candidate_path != abs_chats_dir:
        return None

    if not os.path.isfile(candidate_path) or not candidate_path.lower().endswith(".json"):
        return None

    try:
        with open(candidate_path, "r", encoding="utf-8") as f:
            chat_data = json.load(f)
        if chat_data.get("conversation_id") != cid:
            return None
    except Exception:
        return None

    return candidate_path


def _print_sensitive_matches(
    fpath: str, patterns: dict | None, keywords: dict | None
) -> bool:
    """Scan *fpath* verbosely and print every matched alias with context.

    Returns True if at least one match was found and printed.
    """
    matches = scan_chat_file_verbose(fpath, patterns, keywords)
    if not matches:
        return False
    for alias in sorted(matches):
        for ctx_str, matched_text in matches[alias]:
            print(f"  [{alias}] Found: {matched_text!r}")
            print(f"          Context: {ctx_str}")
            print()
    return True


def _handle_show_sensitive(
    conn, cfg: dict, specific_id: str | None, show_all: bool
):
    """Shared handler for ``--show-sensitive <id>`` and ``--show-sensitive-all``."""
    config_path = SENSITIVE_CONFIG_PATH
    if not os.path.exists(config_path):
        die(f"Sensitive patterns config missing at '{config_path}'.")

    patterns, keywords = load_sensitive_rules(config_path)
    if not patterns and not keywords:
        die("No patterns or keywords defined in sensitive config — nothing to scan.")

    chats_dir = cfg["paths"]["chats_dir"]

    if specific_id:
        fpath = _resolve_chat_file(specific_id, conn, chats_dir)
        if not fpath:
            die(
                f"Chat '{specific_id}' not found in database or its source file "
                f"is missing / invalid."
            )
        chat_row = conn.execute(
            "SELECT conversation_id, title FROM chats WHERE conversation_id = ?",
            (specific_id,),
        ).fetchone()
        title = chat_row["title"] if chat_row else ""
        print(f"\nSensitive matches for '{specific_id}' ({title}):")
        print(f"  File: {fpath}")
        print()
        if not _print_sensitive_matches(fpath, patterns, keywords):
            print("  (no sensitive matches found)")
        return

    # show_all
    if not os.path.exists(MANIFEST_PATH):
        die(
            f"Review manifest missing at '{MANIFEST_PATH}'. "
            f"Run 'python review_chats.py' first."
        )
    try:
        rows = read_and_validate_manifest(MANIFEST_PATH)
    except Exception as e:
        die(f"Manifest validation failed: {e}")

    flagged = [r for r in rows if r.get("flags", "").strip()]
    if not flagged:
        log("No flagged chats found in review manifest.")
        return

    for r in flagged:
        cid = r["conversation_id"]
        fpath = _resolve_chat_file(cid, conn, chats_dir)
        if not fpath:
            continue
        title = r.get("title", "")
        print(f"\n{'=' * 72}")
        print(f"Chat: {title}  ({cid})")
        print(f"File: {fpath}")
        print(f"{'=' * 72}")
        print()
        if not _print_sensitive_matches(fpath, patterns, keywords):
            print("  (no sensitive matches found)")


def run_mask_sensitive(conn, cfg, apply_changes=False):
    """Preview or apply sensitive-info masking on reviewed KEEP chats with flags.

    Preview mode (default): prints aggregated per-alias counts grouped by
    pattern vs. keyword origin, without modifying any files.
    Apply mode (``apply_changes=True``): redacts matched spans in chat JSON
    files, updates the DB (content hash, mtime), and rewrites the manifest
    with the new hashes and cleared flags.
    """
    config_path = SENSITIVE_CONFIG_PATH
    if not os.path.exists(config_path):
        die(f"Sensitive patterns config missing at '{config_path}'.")
    if not os.path.exists(MANIFEST_PATH):
        die(f"Review manifest missing at '{MANIFEST_PATH}'. Run 'python review_chats.py' first.")

    patterns, keywords = load_sensitive_rules(config_path)
    if not patterns and not keywords:
        die("No patterns or keywords defined in sensitive config — nothing to mask.")

    try:
        rows = read_and_validate_manifest(MANIFEST_PATH)
    except Exception as e:
        die(f"Manifest validation failed: {e}")

    candidates = [
        r for r in rows
        if r["reviewed"] == "YES" and r["action"] == "KEEP" and r.get("flags", "").strip()
    ]
    if not candidates:
        log("No reviewed KEEP chats with sensitive flags to mask.")
        return

    chats_dir = cfg["paths"]["chats_dir"]
    sync_chats_to_db(conn, chats_dir)

    pattern_aliases = set(patterns.keys()) if patterns else set()
    keyword_aliases = set(keywords.keys()) if keywords else set()

    # ── Preview mode ─────────────────────────────────────────────────────
    if not apply_changes:
        preview_pattern: dict[str, tuple[str, int]] = {}
        preview_keyword: dict[str, int] = {}

        for r in candidates:
            cid = r["conversation_id"]
            fpath = _resolve_chat_file(cid, conn, chats_dir)
            if not fpath:
                continue
            matches = scan_chat_file_verbose(fpath, patterns, keywords)
            seen_in_chat = set()
            for alias, match_list in matches.items():
                if alias in seen_in_chat:
                    continue
                seen_in_chat.add(alias)
                example = match_list[0][1]
                if alias in pattern_aliases:
                    if alias == "credit_card":
                        digits = re.sub(r"[ -]", "", example)
                        example = f"...{digits[-4:]}"
                    if alias not in preview_pattern:
                        preview_pattern[alias] = (example, 0)
                    prev_example, prev_count = preview_pattern[alias]
                    preview_pattern[alias] = (prev_example, prev_count + 1)
                elif alias in keyword_aliases:
                    preview_keyword[alias] = preview_keyword.get(alias, 0) + 1

        if preview_pattern:
            print(
                "Pattern-based matches — regexes can false-positive, "
                "review before applying:"
            )
            for alias in sorted(preview_pattern):
                example, count = preview_pattern[alias]
                print(f"  [{alias}] {example} -> {count} chat(s)")
            print()

        if preview_keyword:
            print(
                "Keyword-based matches — exact literal values from "
                "your own config:"
            )
            for alias in sorted(preview_keyword):
                count = preview_keyword[alias]
                print(f"  [{alias}] -> {count} chat(s)")
            print()

        log(f"{len(candidates)} chat(s) would be masked.")
        log("Dry run — no files modified. Re-run with --apply to mask these chats.")
        return

    # ── Apply mode ────────────────────────────────────────────────────────
    masked: dict[str, tuple[str, dict]] = {}
    cleared_cids: set[str] = set()
    nothing_to_mask = 0

    for r in candidates:
        cid = r["conversation_id"]
        fpath = _resolve_chat_file(cid, conn, chats_dir)
        if not fpath:
            log(f"  Skipping {cid} — source file not found.")
            cleared_cids.add(cid)
            continue

        result = mask_chat_file(fpath, patterns, keywords)
        if result is None:
            nothing_to_mask += 1
            log(
                f"  Nothing to mask for {cid} — flagged text no longer "
                f"present or was a false positive."
            )
            cleared_cids.add(cid)
        else:
            masked[cid] = (fpath, result)

    # Batch fixup: sync DB then restore correct mtime
    if masked:
        sync_chats_to_db(conn, chats_dir)

        for cid, (fpath, masked_chat) in masked.items():
            ts_str = masked_chat.get("updated_at", "")
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    ts = dt.timestamp()
                    os.utime(fpath, (ts, ts))
                    conn.execute(
                        "UPDATE chats SET file_mtime = ? WHERE conversation_id = ?",
                        (ts, cid),
                    )
                except (ValueError, TypeError):
                    pass
        conn.commit()

    # Update manifest rows
    for r in candidates:
        cid = r["conversation_id"]
        if cid in masked:
            fpath, masked_chat = masked[cid]
            r["content_hash"] = chat_fingerprint(masked_chat)
            r["title"] = truncate_title(masked_chat.get("title", ""))
            new_flags = scan_chat_file(fpath, patterns, keywords)
            r["flags"] = new_flags
            if new_flags:
                log(
                    f"  ⚠ Warning: {cid} still has flags after masking: "
                    f"{new_flags}"
                )
        elif cid in cleared_cids:
            # Re-scan to refresh flags (e.g. Luhn false-positive now excluded)
            fpath = _resolve_chat_file(cid, conn, chats_dir)
            if fpath:
                r["flags"] = scan_chat_file(fpath, patterns, keywords)
            else:
                r["flags"] = ""
        # reviewed and action: leave untouched

    write_manifest_atomically(rows, MANIFEST_PATH)

    masked_count = len(masked)
    log(f"Masked {masked_count} chat(s).")
    if nothing_to_mask:
        log(f"Skipped {nothing_to_mask} chat(s) — nothing to mask.")
    if masked_count:
        log("")
        log(
            "Their content_hash changed, so the next\n"
            "`classify_chats.py` run will regenerate summaries from the "
            "redacted text,\n"
            "and `obsidian_layout.py` will rewrite their vault notes. Run "
            "both before\n"
            "opening the vault if you want the summaries/notes to reflect "
            "the masking."
        )


def main():
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("-") and a != "--show-sensitive"}

    # --show-sensitive consumes the next argument as conversation_id
    show_sensitive_id = None
    for i, a in enumerate(args):
        if a == "--show-sensitive" and i + 1 < len(args):
            show_sensitive_id = args[i + 1]
            break

    show_sensitive_all = "--show-sensitive-all" in flags

    if show_sensitive_id or show_sensitive_all:
        cfg = load_config()
        conn = get_db_connection(cfg)
        try:
            _handle_show_sensitive(conn, cfg, show_sensitive_id, show_sensitive_all)
        finally:
            conn.close()
        return

    if "--mark-reviewed" in flags:
        mark_all_reviewed(yes_flag="-y" in flags)
        return

    mask_sensitive = "--mask-sensitive" in flags
    apply_mask = "--apply" in flags

    if mask_sensitive:
        cfg = load_config()
        conn = get_db_connection(cfg)
        try:
            run_mask_sensitive(conn, cfg, apply_changes=apply_mask)
        finally:
            conn.close()
        return

    scan_sensitive = "--scan-sensitive" in flags

    cfg = load_config()
    conn = get_db_connection(cfg)

    try:
        generate_or_refresh_manifest(conn, cfg, scan_sensitive=scan_sensitive)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
