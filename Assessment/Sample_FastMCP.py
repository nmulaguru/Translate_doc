import logging
import time
import asyncio
import sqlite3
import json
import math
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Optional
from pathlib import Path
from fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── SQL Write Guard (Pydantic + regex, defense-in-depth over SQLite mode=ro) ─
#
# Two-layer protection:
#   Layer 1 — Connection:  _get_db_connection() opens with URI ?mode=ro so the
#             SQLite driver refuses any write at the OS/VFS level.
#   Layer 2 — Application: _safe_execute() validates every dynamically-built SQL
#             string against SqlReadGuard before the call reaches the driver.
#             This catches bugs where a string-concat accidentally produces a
#             write statement before it ever hits the DB.

_WRITE_SQL_RE = re.compile(
    r"""^\s*(
        INSERT\b  | UPDATE\b   | DELETE\b  | DROP\b   |
        ALTER\b   | CREATE\b   | REPLACE\b | TRUNCATE\b |
        ATTACH\b  | DETACH\b   | VACUUM\b  |
        PRAGMA\s+\w+\s*=
    )""",
    re.IGNORECASE | re.VERBOSE,
)


class SqlReadGuard(BaseModel):
    """Pydantic model that rejects any SQL string that is not a SELECT/WITH query.

    Raises ValidationError (caught by _safe_execute and re-raised as ValueError)
    so callers always see a plain ValueError — no Pydantic internals leak out.
    """
    sql: str

    @field_validator("sql")
    @classmethod
    def must_be_select(cls, v: str) -> str:
        stripped = v.strip()
        if _WRITE_SQL_RE.match(stripped):
            raise ValueError(
                f"SQL write guard: non-SELECT statement rejected — "
                f"{stripped[:80]!r}"
            )
        if not re.match(r"^\s*(SELECT|WITH)\b", stripped, re.IGNORECASE):
            raise ValueError(
                f"SQL write guard: only SELECT/WITH queries allowed — "
                f"{stripped[:60]!r}"
            )
        return v


class SearchQueryInput(BaseModel):
    """Pydantic model that validates search_documents input parameters.

    Applied at the entry point of the search_documents MCP tool so bad
    inputs are rejected with a clear message before any SQL is executed.
    """
    query: str
    top_k: int = 20
    offset: int = 0
    exhaustive: bool = False

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be empty")
        if len(v) > 1000:
            raise ValueError("query too long (max 1000 chars)")
        # Detect obvious SQL injection attempts embedded in the search query.
        # Parameterised queries already prevent execution, but this surfaces a
        # clear error immediately rather than passing through to the DB layer.
        if re.search(
            r";\s*(DROP|INSERT|UPDATE|DELETE|ALTER|CREATE)\b",
            v, re.IGNORECASE,
        ):
            raise ValueError("query contains a suspicious SQL fragment")
        return v

    @field_validator("top_k")
    @classmethod
    def validate_top_k(cls, v: int) -> int:
        if not 1 <= v <= 100_000:
            raise ValueError(f"top_k must be between 1 and 100 000, got {v}")
        return v

    @field_validator("offset")
    @classmethod
    def validate_offset(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"offset must be >= 0, got {v}")
        return v


def _safe_execute(cursor_or_conn, sql: str, params=()):
    """Execute SQL after Pydantic + regex read-only validation.

    Works with both sqlite3.Cursor and sqlite3.Connection objects (both expose
    .execute()). Raises ValueError — never ValidationError — on rejection so
    callers see a consistent error type.
    """
    try:
        SqlReadGuard(sql=sql)
    except Exception as exc:  # catches pydantic.ValidationError
        raise ValueError(str(exc)) from exc
    return cursor_or_conn.execute(sql, params)

# Thread pool for CPU-bound BM25 scoring and blocking DB ops
_executor = ThreadPoolExecutor(max_workers=16)

# ── Database Configuration ───────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "fake_database.db"
SEARCH_INDEX_PATH = DB_PATH.parent / "hermes_search_index.db"

# ── Initialise FastMCP Server ────────────────────────────────────────────────
mcp = FastMCP("AICloudDrive Tools")

# Containers with more matching docs than this threshold are auto-paginated (page_size=10000,
# page=1) when page_size is omitted — prevents OOM on 1M+ doc containers.
_AUTO_PAGINATE_THRESHOLD = 50_000


# ── Database Helper Functions ──────────────────────────────────────────────

def _get_db_connection() -> sqlite3.Connection:
    """Get a READ-ONLY connection to the assessment database.

    Opens via SQLite URI with mode=ro so the driver rejects any write attempt
    (INSERT/UPDATE/DELETE/DROP) at the connection level — regardless of what
    any generated code tries to do. fake_database.db is ground-truth data and
    must never be modified.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found at {DB_PATH}.")
    uri = DB_PATH.as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _validate_container_exists(container_id: str) -> bool:
    """Validate that a container exists in the database."""
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM documents WHERE container_id = ?", (container_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0


def _validate_document_exists(content_id: str, container_id: Optional[str] = None) -> bool:
    """Validate that a document exists in the database."""
    conn = _get_db_connection()
    cursor = conn.cursor()

    if container_id:
        cursor.execute(
            "SELECT COUNT(*) FROM documents WHERE content_id = ? AND container_id = ?",
            (content_id, container_id)
        )
    else:
        cursor.execute("SELECT COUNT(*) FROM documents WHERE content_id = ?", (content_id,))

    count = cursor.fetchone()[0]
    conn.close()
    return count > 0


def _get_container_for_document(content_id: str) -> Optional[str]:
    """Get the container_id for a given content_id."""
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT container_id FROM documents WHERE content_id = ?", (content_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def _get_all_containers() -> list[str]:
    """Get list of all container IDs in the database."""
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT container_id FROM documents ORDER BY container_id")
    containers = [row[0] for row in cursor.fetchall()]
    conn.close()
    return containers


def _redact_pii_text(pii_entities: list) -> list:
    """Strip the actual sensitive text from PII entities before any LLM-facing response.

    Keeps type, page, and confidence (useful for compliance reporting) but replaces
    the raw sensitive value with [REDACTED] so it never appears in LLM context.
    """
    redacted = []
    for e in pii_entities:
        if not isinstance(e, dict):
            continue
        redacted.append({
            "type": e.get("type", "UNKNOWN"),
            "text": "[REDACTED]",
            "page": e.get("page"),
            "confidence": e.get("confidence"),
        })
    return redacted


def _get_documents_by_filters(
    container_id: Optional[str] = None,
    category: Optional[str] = None,
    language: Optional[str] = None,
    status: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> list[dict]:
    """Get documents with optional filters. Uses parameterised LIMIT/OFFSET (no SQL injection)."""
    conn = _get_db_connection()
    cursor = conn.cursor()

    query = """
        SELECT content_id, document_name, page_count, size_bytes,
               language, uploaded_at, status, category, pii_count,
               file_extension, classification_category, classification_subcategory,
               classification_confidence, classification_document_type,
               pii_data, created_at, updated_at
        FROM documents
        WHERE 1=1
    """
    params: list = []

    if container_id:
        query += " AND container_id = ?"
        params.append(container_id)
    if category:
        query += " AND category = ?"
        params.append(category)
    if language:
        query += " AND language = ?"
        params.append(language)
    if status:
        query += " AND status = ?"
        params.append(status)

    query += " ORDER BY content_id"

    if limit is not None:
        query += " LIMIT ?"   # parameterised — no f-string injection
        params.append(limit)
    if offset is not None:
        query += " OFFSET ?"
        params.append(offset)

    _safe_execute(cursor, query, params)
    rows = cursor.fetchall()
    conn.close()

    result = []
    for row in rows:
        try:
            pii_types = list({e["type"] for e in json.loads(row[14] or "[]") if isinstance(e, dict) and "type" in e})
        except (json.JSONDecodeError, TypeError):
            pii_types = []
        result.append({
            "documentId": row[0], "documentName": row[1], "pageCount": row[2],
            "size": row[3], "language": row[4], "uploadedAt": row[5],
            "status": row[6], "category": row[7], "piiCount": row[8],
            "fileExtension": row[9],
            "classificationCategory": row[10], "classificationSubcategory": row[11],
            "classificationConfidence": row[12], "classificationDocumentType": row[13],
            "piiTypes": pii_types,
            "createdAt": row[15], "updatedAt": row[16],
        })
    return result


def _count_documents(container_id: str) -> int:
    """Return document count for a container in O(1) — never loads rows into Python."""
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM documents WHERE container_id = ?", (container_id,))
    n = cursor.fetchone()[0]
    conn.close()
    return n


def _count_documents_filtered(
    container_id: str,
    language: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
) -> int:
    """Filtered COUNT(*) pushed to SQLite — O(1) with index, never loads rows into Python.

    When filters are applied this gives the correct filtered total so page_info.has_more
    and auto-pagination thresholds are accurate.
    """
    conn = _get_db_connection()
    cursor = conn.cursor()
    q = "SELECT COUNT(*) FROM documents WHERE container_id = ?"
    params: list = [container_id]
    if language:
        q += " AND language = ?"
        params.append(language)
    if category:
        q += " AND category = ?"
        params.append(category)
    if status:
        q += " AND status = ?"
        params.append(status)
    n = _safe_execute(cursor, q, params).fetchone()[0]
    conn.close()
    return n


def _get_document_insights(content_id: str, model_filter: Optional[str] = None) -> list[dict]:
    """Retrieve AI insights for a document from the database."""
    conn = _get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            classification_category, classification_subcategory,
            classification_confidence, classification_document_type,
            summary, pii_data, pii_count, keywords
        FROM documents
        WHERE content_id = ?
    """, (content_id,))

    row = cursor.fetchone()
    conn.close()

    if not row:
        raise ValueError(f"Document not found: {content_id}")

    # Build insights from database fields
    all_insights = [
        {
            "name": "CLASSIFICATION",
            "status": "SUCCESS",
            "data": {
                "category": row[0],
                "subcategory": row[1],
                "confidence": row[2],
                "document_type": row[3]
            },
            "error": None
        },
        {
            "name": "SUMMARIZATION",
            "status": "SUCCESS",
            "data": row[4],
            "error": None
        },
        {
            "name": "REDACTION",
            "status": "SUCCESS",
            "data": {
                "pii_found": _redact_pii_text(json.loads(row[5])),
                "total_pii_count": row[6]
            },
            "error": None
        },
        {
            "name": "KEYWORDS",
            "status": "SUCCESS",
            "data": {
                "keywords": json.loads(row[7])
            },
            "error": None
        }
    ]

    # Filter by model if specified
    if model_filter:
        model_upper = model_filter.upper()
        # Map user-friendly names to insight names
        model_map = {
            "CLASSIFICATION": "CLASSIFICATION",
            "SUMMARISATION": "SUMMARIZATION",
            "SUMMARIZATION": "SUMMARIZATION",
            "REDACTION": "REDACTION",
            "KEYWORD": "KEYWORDS",
            "KEYWORDS": "KEYWORDS"
        }
        target_name = model_map.get(model_upper, model_upper)
        return [i for i in all_insights if i["name"] == target_name]

    return all_insights


_MODEL_MAP = {
    "CLASSIFICATION": "CLASSIFICATION",
    "SUMMARISATION": "SUMMARIZATION",
    "SUMMARIZATION": "SUMMARIZATION",
    "REDACTION": "REDACTION",
    "KEYWORD": "KEYWORDS",
    "KEYWORDS": "KEYWORDS",
}


def _get_all_insights_bulk(
    container_id: str,
    model_filter: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> dict:
    """Fetch ALL document insights for a container in ONE SQL query.

    Previously get_document_insights() called _get_document_insights(doc_id) inside a loop —
    that was N+1 queries (one connection+query per document). For 9K docs that was
    ~9 seconds, for 1M docs it was hours. This replaces it with a single SELECT.
    Optional limit/offset enable paginated access for very large containers.
    """
    conn = _get_db_connection()
    cursor = conn.cursor()

    q = """
        SELECT content_id,
               classification_category, classification_subcategory,
               classification_confidence, classification_document_type,
               summary, pii_data, pii_count, keywords
        FROM documents
        WHERE container_id = ?
        ORDER BY content_id
    """
    params: list = [container_id]
    if limit is not None:
        q += " LIMIT ?"
        params.append(limit)
    if offset is not None:
        q += " OFFSET ?"
        params.append(offset)

    _safe_execute(cursor, q, params)
    rows = cursor.fetchall()
    conn.close()

    target_name = _MODEL_MAP.get(model_filter.upper(), model_filter.upper()) if model_filter else None

    all_insights: dict = {}
    for row in rows:
        content_id = row[0]
        insight_list = [
            {
                "name": "CLASSIFICATION",
                "status": "SUCCESS",
                "data": {
                    "category": row[1], "subcategory": row[2],
                    "confidence": row[3], "document_type": row[4],
                },
                "error": None,
            },
            {
                "name": "SUMMARIZATION",
                "status": "SUCCESS",
                "data": row[5],
                "error": None,
            },
            {
                "name": "REDACTION",
                "status": "SUCCESS",
                "data": {
                    "pii_found": _redact_pii_text(json.loads(row[6]) if row[6] else []),
                    "total_pii_count": row[7],
                },
                "error": None,
            },
            {
                "name": "KEYWORDS",
                "status": "SUCCESS",
                "data": {"keywords": json.loads(row[8]) if row[8] else []},
                "error": None,
            },
        ]
        if target_name:
            insight_list = [i for i in insight_list if i["name"] == target_name]
        all_insights[content_id] = insight_list

    return all_insights


def _get_documents_metadata(container_id: str, content_id: Optional[str] = None) -> list[dict]:
    """Retrieve document metadata from the database."""
    conn = _get_db_connection()
    cursor = conn.cursor()

    if content_id:
        # Get specific document
        cursor.execute("""
            SELECT content_id, document_name, page_count, size_bytes,
                   language, uploaded_at, status
            FROM documents
            WHERE content_id = ? AND container_id = ?
        """, (content_id, container_id))
    else:
        # Get all documents in container
        cursor.execute("""
            SELECT content_id, document_name, page_count, size_bytes,
                   language, uploaded_at, status
            FROM documents
            WHERE container_id = ?
            ORDER BY content_id
        """, (container_id,))

    rows = cursor.fetchall()
    conn.close()

    documents = []
    for row in rows:
        documents.append({
            "documentId": row[0],
            "documentName": row[1],
            "pageCount": row[2],
            "size": row[3],
            "language": row[4],
            "uploadedAt": row[5],
            "status": row[6]
        })

    return documents


# ── Batch validation ─────────────────────────────────────────────────────────

def _batch_validate_documents(doc_ids: list[str], container_id: str) -> set[str]:
    """Return the subset of doc_ids that actually exist in the container.

    Uses a single SQL IN query instead of one query per document — critical for
    bulk ops where the O(N) approach adds 9 seconds for 9K docs and hours for 1M.
    Chunked at 900 IDs per query to stay under SQLite's SQLITE_MAX_VARIABLE_NUMBER.
    """
    if not doc_ids:
        return set()
    conn = _get_db_connection()
    cursor = conn.cursor()
    valid: set[str] = set()
    chunk_size = 900
    for i in range(0, len(doc_ids), chunk_size):
        chunk = doc_ids[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        cursor.execute(
            f"SELECT content_id FROM documents "
            f"WHERE container_id = ? AND content_id IN ({placeholders})",
            [container_id] + chunk,
        )
        valid.update(row[0] for row in cursor.fetchall())
    conn.close()
    return valid


# ── BM25 Hybrid Search ────────────────────────────────────────────────────────

_BM25_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "my", "your", "their", "our",
    "what", "which", "who", "this", "that", "these", "those", "i", "me",
    "we", "you", "he", "she", "it", "they", "in", "on", "at", "to", "for",
    "of", "and", "or", "but", "with", "by", "from", "all", "please", "show",
    "give", "tell", "find", "get", "list", "any", "about", "how", "when",
    "where", "some", "if", "as", "no", "not",
})


def _tokenize(text: str) -> list[str]:
    return [
        t.lower()
        for t in re.findall(r"\b[a-z]{3,}\b", text.lower())
        if t.lower() not in _BM25_STOPWORDS
    ]


def _bm25_search(
    query: str, container_id: str, top_k: int = 5
) -> tuple[int, list[dict]]:
    """BM25 + keyword-field boost hybrid search. Returns (total_docs, top_k_results).

    Scale fix: previously loaded ALL rows into Python for scoring (fatal at 1M docs).
    Now uses SQL LIKE pre-filter (max-5 query tokens × 4 fields) to cap the candidate
    set at 2000 rows, then runs BM25 only on those. IDF uses the true corpus count
    from a separate COUNT query so scores remain accurate even with the pre-filter.

    Fields scored:
    - BM25 over summary text (primary signal)
    - Keyword phrase boost 3×: query token found in pre-extracted keyword phrases
    - Category boost 2×: query token matches document category
    """
    tokens = _tokenize(query)

    conn = _get_db_connection()
    cursor = conn.cursor()

    # True corpus size for IDF — one fast COUNT, no row loading.
    cursor.execute("SELECT COUNT(*) FROM documents WHERE container_id = ?", (container_id,))
    total: int = cursor.fetchone()[0]

    if not tokens or total == 0:
        conn.close()
        return total, []

    # SQL pre-filter: match any of the top-10 query tokens against key text fields.
    # LIKE '%x%' can't use a B-tree index but runs in C (SQLite engine) — still
    # ~10× faster than loading all rows into Python and tokenising there.
    filter_tokens = tokens[:10]
    like_parts: list[str] = []
    like_params: list = []
    for t in filter_tokens:
        pat = f"%{t}%"
        like_parts.append(
            "(LOWER(summary) LIKE ? OR LOWER(keywords) LIKE ? "
            "OR LOWER(category) LIKE ? OR LOWER(document_name) LIKE ?)"
        )
        like_params.extend([pat, pat, pat, pat])

    sql = (
        "SELECT content_id, document_name, summary, keywords, "
        "category, classification_category, classification_subcategory "
        "FROM documents "
        f"WHERE container_id = ? AND ({' OR '.join(like_parts)}) "
        "LIMIT 2000"
    )
    _safe_execute(cursor, sql, [container_id] + like_params)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return total, []

    # Build candidate corpus for BM25 scoring (≤ 2000 rows).
    doc_texts: list[list[str]] = []
    doc_meta: list[dict] = []
    for row in rows:
        cid, name, summary, kw_json, cat, cls_cat, cls_sub = row
        text_tokens = _tokenize(f"{summary or ''} {cat or ''} {cls_cat or ''} {cls_sub or ''}")
        doc_texts.append(text_tokens)
        kw_phrases: list[str] = []
        try:
            kw_data = json.loads(kw_json or "[]")
            for kw in (kw_data if isinstance(kw_data, list) else []):
                phrase = kw.get("phrase") or kw.get("keyword") or "" if isinstance(kw, dict) else (kw if isinstance(kw, str) else "")
                if phrase:
                    kw_phrases.append(phrase.lower())
        except (json.JSONDecodeError, TypeError):
            pass
        doc_meta.append({"content_id": cid, "document_name": name, "summary": summary, "category": cat, "kw_phrases": kw_phrases})

    # ── BM25 with true corpus N (not candidate N) ────────────────────────────
    k1, b = 1.5, 0.75
    N = total  # corpus-wide document count for accurate IDF
    candidate_n = len(doc_texts)
    avgdl = sum(len(d) for d in doc_texts) / max(candidate_n, 1)

    df: dict[str, int] = Counter()
    for tokens_d in doc_texts:
        seen = set(tokens_d)
        for t in tokens:
            if t in seen:
                df[t] += 1

    scored: list[dict] = []
    for i, tokens_d in enumerate(doc_texts):
        tf = Counter(tokens_d)
        dl = len(tokens_d)
        score = 0.0
        for t in tokens:
            if tf[t] == 0:
                continue
            idf = math.log((N - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5) + 1)
            tf_norm = (tf[t] * (k1 + 1)) / (tf[t] + k1 * (1 - b + b * dl / max(avgdl, 1)))
            score += idf * tf_norm

        meta = doc_meta[i]
        for phrase in meta["kw_phrases"]:
            for t in tokens:
                if t in phrase:
                    score += 3.0
                    break

        cat_tokens = _tokenize(meta["category"] or "")
        for t in tokens:
            if t in cat_tokens:
                score += 2.0

        if score > 0:
            scored.append({**meta, "score": score})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return total, scored[:top_k]


def _build_rag_answer(query: str, results: list[dict], container_id: str, doc_count: int) -> str:
    """Construct a contextual answer from BM25-retrieved document summaries."""
    if not results:
        return (
            f"No documents in container {container_id} ({doc_count} total) contained "
            f"information relevant to: '{query}'. "
            "Try rephrasing the question or check that the relevant documents are in this container."
        )

    parts = [
        f"Based on {len(results)} relevant document(s) retrieved from container "
        f"{container_id} ({doc_count} total documents):\n"
    ]
    for rank, doc in enumerate(results, 1):
        parts.append(
            f"\n**{rank}. {doc['document_name']}**"
            f" (category: {doc.get('category') or 'unknown'}, "
            f"relevance score: {doc['score']:.1f})"
        )
        summary = doc.get("summary") or ""
        if summary:
            # Trim to 600 chars to keep response concise
            parts.append(f"   Summary: {summary[:600]}{'...' if len(summary) > 600 else ''}")

    parts.append(
        f"\n\n*(Retrieved via BM25 hybrid search — "
        f"{len(results)} of {doc_count} documents matched the query tokens)*"
    )
    return "\n".join(parts)


# ── Hybrid Search Index: FTS5 BM25 + TF-IDF cosine re-rank via RRF ───────────
#
# Architecture:
#  1. SQLite FTS5 (C, inverted index) retrieves top BM25 candidates — milliseconds even at 1M docs.
#  2. TF-IDF cosine re-ranks the small candidate set (~60 docs) for query relevance.
#  3. Reciprocal Rank Fusion fuses both rankings: score = α/(k+r_bm25) + (1-α)/(k+r_cos).
#
# The index lives in hermes_search_index.db (separate from the source DB) and is built once
# on first call to search_documents(), then reused across requests.
# ─────────────────────────────────────────────────────────────────────────────

_index_lock = threading.Lock()
_index_built: bool = False
_fts5_ok: bool | None = None  # None = not yet checked


def _is_fts5_available() -> bool:
    """Check whether this SQLite installation has FTS5 compiled in."""
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE _fts5chk USING fts5(x)")
        c.close()
        return True
    except sqlite3.OperationalError:
        return False


def _do_build_index_sync() -> None:
    """Build the FTS5 index from fake_database.db. Caller must hold _index_lock.

    Streams rows in 10K batches so RAM stays flat regardless of corpus size —
    no fetchall() that would load 1M rows (and a second copy in fts/meta lists)
    into Python memory simultaneously.
    """
    global _index_built
    src = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)

    idx = sqlite3.connect(str(SEARCH_INDEX_PATH))
    idx.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            content_id UNINDEXED,
            container_id UNINDEXED,
            document_name,
            searchable_text,
            tokenize='unicode61'
        )
    """)
    idx.execute("""
        CREATE TABLE IF NOT EXISTS doc_meta (
            content_id TEXT PRIMARY KEY,
            container_id TEXT NOT NULL,
            document_name TEXT,
            category TEXT,
            summary TEXT
        )
    """)
    idx.execute("CREATE INDEX IF NOT EXISTS idx_dm_cid ON doc_meta(container_id)")
    idx.execute("DELETE FROM docs_fts")
    idx.execute("DELETE FROM doc_meta")

    cursor = src.execute("""
        SELECT content_id, container_id, document_name, summary, keywords,
               classification_category, classification_subcategory, category
        FROM documents ORDER BY rowid
    """)

    total = 0
    _BATCH = 10_000
    while True:
        rows = cursor.fetchmany(_BATCH)
        if not rows:
            break
        fts_batch: list[tuple] = []
        meta_batch: list[tuple] = []
        for row in rows:
            cid, cont_id, name, summary, kw_json, cls_cat, cls_sub, cat = row
            kw_text = ""
            try:
                kw_data = json.loads(kw_json or "[]")
                phrases = []
                for kw in (kw_data if isinstance(kw_data, list) else []):
                    p = (kw.get("phrase") or kw.get("keyword") or "") if isinstance(kw, dict) else (kw if isinstance(kw, str) else "")
                    if p:
                        phrases.append(p)
                kw_text = " ".join(phrases)
            except (json.JSONDecodeError, TypeError):
                pass
            searchable = " ".join(filter(None, [
                name or "", summary or "", kw_text,
                cls_cat or "", cls_sub or "", cat or "",
            ]))
            fts_batch.append((cid, cont_id, name or "", searchable))
            meta_batch.append((cid, cont_id, name, cat, summary))
        idx.executemany(
            "INSERT INTO docs_fts(content_id, container_id, document_name, searchable_text) VALUES (?, ?, ?, ?)",
            fts_batch,
        )
        idx.executemany(
            "INSERT OR REPLACE INTO doc_meta(content_id, container_id, document_name, category, summary) VALUES (?, ?, ?, ?, ?)",
            meta_batch,
        )
        total += len(rows)

    idx.commit()
    src.close()
    idx.close()
    _index_built = True
    logger.info(f"[SEARCH_INDEX] FTS5 index ready — {total} documents indexed")


def _build_or_reuse_index_sync() -> None:
    """Thread-safe entry point: reuse existing index if valid, else build from scratch."""
    global _index_built
    with _index_lock:
        if _index_built:
            return
        if SEARCH_INDEX_PATH.exists():
            try:
                chk = sqlite3.connect(str(SEARCH_INDEX_PATH))
                cnt = chk.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
                chk.close()
                if cnt > 0:
                    _index_built = True
                    logger.info(f"[SEARCH_INDEX] Reusing existing FTS5 index ({cnt} docs)")
                    return
            except Exception:
                pass  # Corrupt or missing table — rebuild
        logger.info("[SEARCH_INDEX] Building FTS5 search index (first startup)…")
        _do_build_index_sync()


async def _ensure_index_built() -> bool:
    """Ensure the FTS5 index is ready. Returns True if ready, False if FTS5 unavailable.

    Uses get_running_loop() (correct inside async) instead of get_event_loop().
    Catches any build exception so the tool returns a clean error rather than
    an ExceptionGroup that confuses FastMCP's TaskGroup error handler.
    """
    global _fts5_ok
    loop = asyncio.get_running_loop()
    if _fts5_ok is None:
        _fts5_ok = await loop.run_in_executor(_executor, _is_fts5_available)
        if not _fts5_ok:
            logger.warning("[SEARCH_INDEX] FTS5 not available in this SQLite build — falling back to BM25")
    if not _fts5_ok:
        return False
    if _index_built:
        return True
    try:
        await loop.run_in_executor(_executor, _build_or_reuse_index_sync)
    except Exception as exc:
        logger.error(f"[SEARCH_INDEX] Index build failed ({type(exc).__name__}): {exc}")
        return False
    return _index_built


def _fts5_tokenize_query(query: str) -> list[str]:
    """Tokenize a query for FTS5 expression building.

    Differs from _tokenize:
    - Includes numbers and 2-char terms (catches 'EU', 'IP', 'AI', 'net 60', 'Article 17')
    - Caps at 20 tokens (was 10 — long queries no longer silently lose terms)
    Stopwords are still removed; FTS5 handles operator safety via double-quoting.
    """
    tokens = [
        t for t in re.findall(r"\b[a-z0-9]{2,}\b", query.lower())
        if t not in _BM25_STOPWORDS
    ]
    return tokens[:20]


def _fts5_count_sync(tokens: list[str], container_id: Optional[str]) -> int:
    """Return the accurate total_matched count for a query.

    Problem with naive OR count: a query like "indemnification clause liability"
    produces an OR expression that matches any doc containing ANY of those words.
    "clause" alone matches 14% of the corpus (all legal docs), inflating the count
    far beyond the number of docs actually about the core concept.

    Fix: compute per-token document frequency against the index, drop tokens that
    appear in more than _HIGH_FREQ_RATIO of the corpus (they are generic noise for
    counting purposes), then COUNT with the remaining specific tokens only.
    The retrieval side (_fts5_search_sync) still uses OR-of-all for broad recall
    and re-ranks — this change only affects the reported count.
    """
    _HIGH_FREQ_RATIO = 0.12   # tokens in >12% of corpus are too generic to count on
    if not tokens or not SEARCH_INDEX_PATH.exists():
        return 0
    try:
        idx = sqlite3.connect(str(SEARCH_INDEX_PATH))

        total_row = idx.execute("SELECT COUNT(*) FROM docs_fts").fetchone()
        total_docs = (total_row[0] if total_row else 1) or 1
        threshold = total_docs * _HIGH_FREQ_RATIO

        # Per-token document frequency — fast posting-list lookups, no row scan.
        token_dfs: list[tuple[str, int]] = []
        for t in tokens:
            expr = f'"{t}"'
            try:
                if container_id:
                    row = idx.execute(
                        "SELECT COUNT(*) FROM docs_fts WHERE docs_fts MATCH ? AND container_id = ?",
                        (expr, container_id),
                    ).fetchone()
                else:
                    row = idx.execute(
                        "SELECT COUNT(*) FROM docs_fts WHERE docs_fts MATCH ?",
                        (expr,),
                    ).fetchone()
                token_dfs.append((t, row[0] if row else 0))
            except sqlite3.OperationalError:
                token_dfs.append((t, 0))

        # Keep only specific (low-DF) tokens.
        specific = [t for t, df in token_dfs if 0 < df < threshold]
        if not specific:
            # All tokens are generic — fall back to the single least-common one.
            non_zero = [(t, df) for t, df in token_dfs if df > 0]
            specific = [min(non_zero, key=lambda x: x[1])[0]] if non_zero else []
        if not specific:
            idx.close()
            return 0

        count_expr = " OR ".join(f'"{t}"' for t in specific)
        if container_id:
            row = idx.execute(
                "SELECT COUNT(*) FROM docs_fts WHERE docs_fts MATCH ? AND container_id = ?",
                (count_expr, container_id),
            ).fetchone()
        else:
            row = idx.execute(
                "SELECT COUNT(*) FROM docs_fts WHERE docs_fts MATCH ?",
                (count_expr,),
            ).fetchone()
        idx.close()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def _fts5_search_sync(query: str, container_id: Optional[str], limit: Optional[int], offset: int = 0) -> list[dict]:
    """BM25 via SQLite FTS5. Returns results ordered by relevance (rank).

    limit=None means no cap — returns every matching document (used when top_k is large
    enough that the caller wants all results, not just a page).
    `offset` enables pagination for stable page boundaries.
    """
    tokens = _fts5_tokenize_query(query)
    if not tokens or not SEARCH_INDEX_PATH.exists():
        return []
    fts5_expr = " OR ".join(f'"{t}"' for t in tokens)
    try:
        idx = sqlite3.connect(str(SEARCH_INDEX_PATH))
        if limit is None:
            # No cap — return every matching row.
            if container_id:
                rows = idx.execute(
                    """SELECT content_id, container_id, document_name, bm25(docs_fts) AS score
                       FROM docs_fts WHERE docs_fts MATCH ? AND container_id = ?
                       ORDER BY rank""",
                    (fts5_expr, container_id),
                ).fetchall()
            else:
                rows = idx.execute(
                    """SELECT content_id, container_id, document_name, bm25(docs_fts) AS score
                       FROM docs_fts WHERE docs_fts MATCH ?
                       ORDER BY rank""",
                    (fts5_expr,),
                ).fetchall()
        else:
            if container_id:
                rows = idx.execute(
                    """SELECT content_id, container_id, document_name, bm25(docs_fts) AS score
                       FROM docs_fts WHERE docs_fts MATCH ? AND container_id = ?
                       ORDER BY rank LIMIT ? OFFSET ?""",
                    (fts5_expr, container_id, limit, offset),
                ).fetchall()
            else:
                rows = idx.execute(
                    """SELECT content_id, container_id, document_name, bm25(docs_fts) AS score
                       FROM docs_fts WHERE docs_fts MATCH ?
                       ORDER BY rank LIMIT ? OFFSET ?""",
                    (fts5_expr, limit, offset),
                ).fetchall()
        idx.close()
    except sqlite3.OperationalError as e:
        logger.warning(f"[SEARCH_INDEX] FTS5 query error: {e}")
        return []
    return [
        {"content_id": r[0], "container_id": r[1], "document_name": r[2], "bm25_score": r[3]}
        for r in rows
    ]


def _fts5_all_ids_sync(query: str, container_id: Optional[str]) -> list[dict]:
    """Unranked exhaustive match — returns ALL matching document IDs in one scan.

    No ORDER BY, no LIMIT. Use when you need every matching document ID and don't
    care about relevance order (e.g. 'find all docs with X, then translate them all').
    Faster than ranked retrieval for large match sets because SQLite doesn't score.
    """
    tokens = _fts5_tokenize_query(query)
    if not tokens or not SEARCH_INDEX_PATH.exists():
        return []
    fts5_expr = " OR ".join(f'"{t}"' for t in tokens)
    try:
        idx = sqlite3.connect(str(SEARCH_INDEX_PATH))
        if container_id:
            rows = idx.execute(
                "SELECT content_id, container_id, document_name FROM docs_fts "
                "WHERE docs_fts MATCH ? AND container_id = ?",
                (fts5_expr, container_id),
            ).fetchall()
        else:
            rows = idx.execute(
                "SELECT content_id, container_id, document_name FROM docs_fts "
                "WHERE docs_fts MATCH ?",
                (fts5_expr,),
            ).fetchall()
        idx.close()
    except sqlite3.OperationalError as e:
        logger.warning(f"[SEARCH_INDEX] FTS5 exhaustive query error: {e}")
        return []
    return [{"content_id": r[0], "container_id": r[1], "document_name": r[2]} for r in rows]


def _cosine_rerank_sync(query: str, candidates: list[dict]) -> list[tuple[str, float]]:
    """Query-anchored TF-IDF cosine re-rank on the candidate set. Returns [(cid, score)]."""
    if not candidates:
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return [(c["content_id"], 0.0) for c in candidates]

    # Fetch summaries from the index for richer document text
    cids = [c["content_id"] for c in candidates]
    ph = ",".join("?" * len(cids))
    idx = sqlite3.connect(str(SEARCH_INDEX_PATH))
    summary_map = {
        r[0]: r[1] or ""
        for r in idx.execute(
            f"SELECT content_id, summary FROM doc_meta WHERE content_id IN ({ph})", cids
        ).fetchall()
    }
    idx.close()

    doc_tokens: list[list[str]] = [
        _tokenize(f"{c['document_name']} {summary_map.get(c['content_id'], '')}")
        for c in candidates
    ]
    n = len(candidates)

    df: dict[str, int] = {}
    for tks in doc_tokens:
        seen = set(tks)
        for t in set(query_tokens):
            if t in seen:
                df[t] = df.get(t, 0) + 1

    q_tf = Counter(query_tokens)
    q_vec = {t: q_tf[t] * math.log((n + 1) / (df.get(t, 0) + 1)) for t in set(query_tokens)}
    q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0

    results: list[tuple[str, float]] = []
    for i, c in enumerate(candidates):
        tf = Counter(doc_tokens[i])
        dot = d_sq = 0.0
        for t, qv in q_vec.items():
            idf = math.log((n + 1) / (df.get(t, 0) + 1))
            dv = tf.get(t, 0) * idf
            dot += qv * dv
            d_sq += dv * dv
        cosine = dot / (q_norm * (math.sqrt(d_sq) or 1.0))
        results.append((c["content_id"], cosine))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


def _rrf_fuse(
    bm25: list[dict],
    cosine: list[tuple[str, float]],
    meta: dict[str, dict],
    top_k: int,
    k: int = 60,
    alpha: float = 0.5,
) -> list[dict]:
    """Reciprocal Rank Fusion: score = α/(k+rank_bm25) + (1-α)/(k+rank_cosine)."""
    scores: dict[str, float] = {}
    for rank, doc in enumerate(bm25):
        cid = doc["content_id"]
        scores[cid] = scores.get(cid, 0.0) + alpha / (k + rank + 1)
    for rank, (cid, _) in enumerate(cosine):
        scores[cid] = scores.get(cid, 0.0) + (1 - alpha) / (k + rank + 1)
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    return [
        {
            "rank": i + 1,
            "content_id": cid,
            "container_id": meta.get(cid, {}).get("container_id", ""),
            "document_name": meta.get(cid, {}).get("document_name", ""),
            "relevance_score": round(score, 6),
        }
        for i, (cid, score) in enumerate(ranked)
    ]


def _exhaustive_sql_search_sync(query: str, container_id: Optional[str]) -> list[dict]:
    """Exhaustive fallback: scans ALL rows via LIKE, no LIMIT, no pre-filter cap.

    Used when FTS5 index is unavailable and exhaustive=True is requested.
    Returns every document whose name, summary, keywords, or category contains
    any query token. Accuracy over speed — this is the correct behaviour for
    'find ALL documents matching X'.
    """
    tokens = _tokenize(query)
    if not tokens:
        return []

    conn = _get_db_connection()
    cursor = conn.cursor()

    like_parts: list[str] = []
    like_params: list = []
    for t in tokens:
        pat = f"%{t}%"
        like_parts.append(
            "(LOWER(summary) LIKE ? OR LOWER(keywords) LIKE ? "
            "OR LOWER(category) LIKE ? OR LOWER(document_name) LIKE ?)"
        )
        like_params.extend([pat, pat, pat, pat])

    where = " OR ".join(like_parts)
    base = (
        "SELECT content_id, container_id, document_name FROM documents "
        f"WHERE ({where})"
    )
    params: list = like_params[:]

    if container_id:
        base += " AND container_id = ?"
        params.append(container_id)

    rows = _safe_execute(cursor, base, params).fetchall()
    conn.close()
    return [{"content_id": r[0], "container_id": r[1], "document_name": r[2]} for r in rows]


async def _bm25_search_tool_fallback(
    query: str, container_id: Optional[str], top_k: int, loop,
    exhaustive: bool = False,
) -> dict:
    """BM25 LIKE-based fallback used when FTS5 is unavailable or the FTS5 index fails to build.

    exhaustive=True: full table scan via SQL LIKE with NO LIMIT — returns every matching
    document. total_matched is the true count. No records are dropped.

    exhaustive=False: scored BM25 over up to 2000 LIKE-filtered candidates, returns top_k.
    """
    if exhaustive:
        # Full scan — no cap, no scoring, every matching row returned.
        all_docs = await loop.run_in_executor(
            _executor, _exhaustive_sql_search_sync, query, container_id
        )
        matches = [
            {
                "rank": i + 1,
                "content_id": d["content_id"],
                "container_id": d["container_id"],
                "document_name": d["document_name"],
                "relevance_score": None,
            }
            for i, d in enumerate(all_docs)
        ]
        return {
            "query": query,
            "container_id": container_id,
            "total_matched": len(matches),
            "exhaustive": True,
            "content_ids": [m["content_id"] for m in matches],
            "matches": matches,
            "results_capped": False,
            "next_offset": None,
        }

    # Ranked path — BM25 over pre-filtered candidates, capped at top_k.
    containers = [container_id] if container_id else _get_all_containers()
    all_results: list[dict] = []
    for cid in containers:
        _, results = await loop.run_in_executor(_executor, _bm25_search, query, cid, top_k)
        for r in results:
            all_results.append({
                "rank": 0,
                "content_id": r["content_id"],
                "container_id": cid,
                "document_name": r.get("document_name", ""),
                "relevance_score": round(r.get("score", 0.0), 6),
            })
    all_results.sort(key=lambda x: x["relevance_score"], reverse=True)
    all_results = all_results[:top_k]
    for i, r in enumerate(all_results):
        r["rank"] = i + 1
    return {
        "query": query,
        "container_id": container_id,
        "total_matched": len(all_results),
        "content_ids": [r["content_id"] for r in all_results],
        "matches": all_results,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL: search_documents
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(name="search_documents")
async def search_documents(
    query: Annotated[
        str,
        Field(description=(
            "Natural language query or keywords to search for across document metadata, "
            "summaries, category, and keyword phrases. Examples: 'indemnification clauses', "
            "'payment terms net 60', 'GDPR data processing agreement', 'force majeure'."
        )),
    ],
    container_id: Annotated[
        Optional[str],
        Field(description=(
            "Restrict search to a specific container. Omit (null) to search ALL containers. "
            "Examples: 'container_001', 'container_004'."
        )),
    ] = None,
    top_k: Annotated[
        int,
        Field(
            description=(
                "Maximum matching documents to return per page. Default 20. "
                "Use top_k=10000 when the user says 'find ALL documents' or 'every document with...'. "
                "FTS5 handles large limits natively in milliseconds. "
                "When results_capped=true, paginate using offset to retrieve remaining matches."
            ),
            ge=1,
            le=10000,
        ),
    ] = 20,
    offset: Annotated[
        int,
        Field(
            description=(
                "Skip this many results before returning. Use for pagination when results_capped=true. "
                "Example: first page offset=0, second page offset=10000, third page offset=20000. "
                "Paginated calls use pure BM25 order (no cosine re-rank) for stable page boundaries."
            ),
            ge=0,
        ),
    ] = 0,
    exhaustive: Annotated[
        bool,
        Field(
            description=(
                "When true, returns ALL matching document IDs in one call with no ranking and no limit. "
                "Use this when you need every matching ID (e.g. 'translate ALL docs with X'). "
                "Returns content_ids and matches without relevance_score. Ignores top_k and offset. "
                "Faster than ranked retrieval for large match sets (no BM25 scoring overhead)."
            ),
        ),
    ] = False,
) -> dict:
    """Fast hybrid document search — returns matching content_ids in milliseconds.

    Architecture: SQLite FTS5 BM25 retrieval → TF-IDF cosine re-rank (small sets only) → RRF fusion.
    Falls back to the LIKE-based BM25 implementation when FTS5 is unavailable.

    Scale note:
    - top_k <= 100: FTS5 retrieves top_k*3 candidates, cosine re-ranks, RRF fuses.
    - top_k > 100:  FTS5 retrieves top_k directly (no artificial cap), cosine skipped (fast path).
      This allows exhaustive retrieval of thousands of matching documents in one call.

    Use this INSTEAD of get_document_insights to FIND documents by topic/keyword.

    Returns:
        {
          "query": "...", "container_id": null | "container_001",
          "total_matched": 3018,
          "content_ids": ["container_001_doc_000042", ...],
          "matches": [{"rank": 1, "content_id": "...", "container_id": "...",
                       "document_name": "...", "relevance_score": 0.95}, ...]
        }
    """
    try:
        # Pydantic validates query length, injection patterns, top_k range, offset ≥ 0.
        _validated = SearchQueryInput(
            query=query, top_k=top_k, offset=offset, exhaustive=exhaustive
        )
        query = _validated.query          # use the stripped version
        top_k = _validated.top_k
        offset = _validated.offset

        if container_id and not _validate_container_exists(container_id):
            available = _get_all_containers()
            raise ValueError(f"Container not found: {container_id}. Available: {', '.join(available)}")

        loop = asyncio.get_running_loop()

        # Ensure FTS5 index is ready; returns False if FTS5 is unavailable
        index_ready = await _ensure_index_built()
        if not index_ready:
            logger.info("[MCP_TOOL] search_documents | FTS5 unavailable — using BM25 fallback")
            return await _bm25_search_tool_fallback(query, container_id, top_k, loop, exhaustive=exhaustive)

        # ── FTS5 path ──────────────────────────────────────────────────────────
        fts5_tokens = _fts5_tokenize_query(query)
        if not fts5_tokens:
            # All query words are stop-words — FTS5 has nothing to match on.
            # Surface a clear error so the planner routes to the right tool instead
            # of silently returning 0 results and confusing the synthesizer.
            raise ValueError(
                "The query contains only common stop words (e.g. 'list', 'all', 'show', 'find', 'get') "
                "and cannot be searched with search_documents. "
                "To enumerate or list documents use get_active_documents_metadata instead "
                "(supports language/category/status filters and pagination)."
            )

        # ── Exhaustive mode: one unranked scan, all IDs, no cap ──────────────
        if exhaustive:
            all_docs = await loop.run_in_executor(
                _executor, _fts5_all_ids_sync, query, container_id
            )
            content_ids = [d["content_id"] for d in all_docs]
            logger.info(
                f"[MCP_TOOL_RESULT] search_documents (exhaustive) | query='{query[:60]}' | "
                f"container={container_id or 'all'} | total={len(content_ids)}"
            )
            return {
                "query": query,
                "container_id": container_id,
                "total_matched": len(content_ids),
                "exhaustive": True,
                "content_ids": content_ids,
                "matches": [{"rank": i + 1, "content_id": d["content_id"],
                             "container_id": d["container_id"],
                             "document_name": d["document_name"],
                             "relevance_score": None} for i, d in enumerate(all_docs)],
                "results_capped": False,
                "next_offset": None,
            }

        # ── Ranked path (default) ─────────────────────────────────────────────
        # Paginated calls (offset > 0) use pure BM25 order for stable page boundaries.
        # First-page calls (offset == 0, small top_k) get the full cosine+RRF pipeline.
        # candidate_limit is uncapped (None → no LIMIT) when top_k is large, so that
        # "find all" queries with top_k=10000 actually see the full matching set.
        use_rerank = (offset == 0 and top_k <= 100)
        # Large top_k means "give me everything" — remove the LIMIT so FTS5 returns
        # all matching rows, not just the first top_k. Small top_k uses top_k*3 to
        # give the cosine re-ranker enough candidates to work with.
        if use_rerank:
            candidate_limit: Optional[int] = top_k * 3
        elif top_k >= 1000:
            candidate_limit = None  # no cap — return every match
        else:
            candidate_limit = top_k

        # Run the ranked retrieval and the true COUNT in parallel.
        bm25_results, true_count = await asyncio.gather(
            loop.run_in_executor(_executor, _fts5_search_sync, query, container_id, candidate_limit, offset),
            loop.run_in_executor(_executor, _fts5_count_sync, fts5_tokens, container_id),
        )

        if not bm25_results:
            logger.info(f"[MCP_TOOL] search_documents | query='{query[:60]}' | no matches")
            return {
                "query": query, "container_id": container_id,
                "total_matched": true_count, "content_ids": [], "matches": [],
                "results_capped": False, "offset": offset,
            }

        if use_rerank:
            cosine_results = await loop.run_in_executor(
                _executor, _cosine_rerank_sync, query, bm25_results
            )
            candidate_meta = {r["content_id"]: r for r in bm25_results}
            fused = _rrf_fuse(bm25_results, cosine_results, candidate_meta, top_k=top_k)
        else:
            # Large top_k or paginated — BM25 order is already good; no cosine overhead.
            fused = [
                {
                    "rank": i + 1 + offset,
                    "content_id": r["content_id"],
                    "container_id": r["container_id"],
                    "document_name": r["document_name"],
                    "relevance_score": round(1.0 / (1.0 + abs(r["bm25_score"])), 4),
                }
                for i, r in enumerate(bm25_results)
            ]

        content_ids = [m["content_id"] for m in fused]

        # results_capped: true when the index has more matches than this page covers.
        results_capped = true_count > (offset + top_k)

        logger.info(
            f"[MCP_TOOL_RESULT] search_documents | query='{query[:60]}' | "
            f"container={container_id or 'all'} | true_count={true_count} | offset={offset} | "
            f"fts5_candidates={len(bm25_results)} | returned={len(fused)} | capped={results_capped}"
        )
        return {
            "query": query,
            "container_id": container_id,
            "total_matched": true_count,
            "offset": offset,
            "content_ids": content_ids,
            "matches": fused,
            "results_capped": results_capped,
            "next_offset": offset + len(fused) if results_capped else None,
        }

    except (ValueError, sqlite3.Error):
        raise  # let FastMCP surface these as clean tool errors
    except Exception as exc:
        # Catch anything else so it never escapes as an ExceptionGroup into FastMCP's TaskGroup
        logger.error(f"[search_documents] Unexpected error: {exc}", exc_info=True)
        raise ValueError(f"search_documents failed: {exc}") from exc


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL: get_document_insights
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(name="get_document_insights")
async def get_document_insights(
    container_id: Annotated[
        str,
        Field(
            description=(
                "The container ID where the document(s) exist. "
                "Examples: 'container_001', 'container_004'"
            ),
        ),
    ],
    model: Annotated[
        Optional[str],
        Field(
            description=(
                "Filter to a specific insight type: 'Classification', 'Summarisation', "
                "'Redaction', or 'Keyword'. Omit for all insight types."
            ),
        ),
    ] = None,
    page_size: Annotated[
        Optional[int],
        Field(
            description=(
                "Documents per page. Omit to return ALL documents (safe up to ~50K). "
                "For 1M+ containers set page_size=10000 and iterate pages. Max 100000."
            ),
            ge=1,
            le=100000,
        ),
    ] = None,
    page: Annotated[
        Optional[int],
        Field(description="1-based page number. Only used when page_size is set. Defaults to 1.", ge=1),
    ] = None,
) -> dict:
    """Retrieves AI-generated insights for documents in a container.

    Supports pagination for 1M+ document containers:
      page 1: page_size=10000, page=1  → insights for docs 1–10000
      page 2: page_size=10000, page=2  → insights for docs 10001–20000
      ...until returned insights count < page_size (last page).

    Returns:
        {container_id, total_documents, insights: {doc_id: [insight, ...]}, page_info?}
    """
    if not container_id:
        raise ValueError("Container ID must be provided.")

    if not _validate_container_exists(container_id):
        available_containers = _get_all_containers()
        raise ValueError(
            f"Container not found: {container_id}. "
            f"Available containers: {', '.join(available_containers)}"
        )

    loop = asyncio.get_running_loop()
    total = await loop.run_in_executor(_executor, _count_documents, container_id)

    # Auto-pagination safety: containers > 50K docs can't safely fetch all insights at once.
    auto_paginated = False
    effective_page_size = page_size
    effective_page = page or 1
    if page_size is None and total > _AUTO_PAGINATE_THRESHOLD:
        effective_page_size = 10_000
        effective_page = 1
        auto_paginated = True
        logger.warning(
            f"[get_document_insights] container {container_id} has {total} docs "
            f"(>{_AUTO_PAGINATE_THRESHOLD}); auto-paginating to page_size=10000 page=1."
        )

    limit: Optional[int] = None
    offset: Optional[int] = None
    if effective_page_size is not None:
        limit = effective_page_size
        offset = (effective_page - 1) * effective_page_size

    logger.info(
        f"[MCP_TOOL_SELECTED] get_document_insights | container: {container_id} | "
        f"total_docs: {total} | model: {model or 'all'} | page_size: {effective_page_size} | page: {effective_page}"
    )

    all_insights = await loop.run_in_executor(
        _executor, _get_all_insights_bulk, container_id, model, limit, offset
    )

    logger.info(
        f"[MCP_TOOL_RESULT] get_document_insights | container: {container_id} | "
        f"retrieved: {len(all_insights)} documents"
    )

    result: dict = {
        "container_id": container_id,
        "total_documents": total,
        "insights": all_insights,
    }
    if effective_page_size is not None:
        result["page_info"] = {
            "page": effective_page,
            "page_size": effective_page_size,
            "returned": len(all_insights),
            "has_more": len(all_insights) == effective_page_size,
        }
    if auto_paginated:
        result["auto_paginated"] = True
        result["auto_paginated_note"] = (
            f"Container has {total} documents (>{_AUTO_PAGINATE_THRESHOLD}). "
            f"Returned page 1 of ~{math.ceil(total / 10000)} (10000 docs). "
            f"Set page_size=10000 and iterate pages to retrieve all insights."
        )
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL: get_active_documents_metadata
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(name="get_active_documents_metadata")
async def get_active_documents_metadata(
    container_id: Annotated[
        str,
        Field(description="The container ID to query. Examples: 'container_001', 'container_004'"),
    ],
    page_size: Annotated[
        Optional[int],
        Field(
            description=(
                "Documents per page. Omit for containers ≤50K docs (returns all). "
                "For 1M+ doc containers set page_size=10000 and iterate pages until "
                "page_info.has_more is false. Max 100000."
            ),
            ge=1,
            le=100000,
        ),
    ] = None,
    page: Annotated[
        Optional[int],
        Field(description="1-based page number. Only used when page_size is set. Defaults to 1.", ge=1),
    ] = None,
    language: Annotated[
        Optional[str],
        Field(
            description=(
                "ISO 639-1 two-letter language code to server-side filter by. "
                "Examples: 'fr' (French), 'de' (German), 'en' (English), 'es' (Spanish), "
                "'it' (Italian), 'ja' (Japanese), 'zh' (Chinese), 'pt' (Portuguese). "
                "CRITICAL: use two-letter codes only — NOT 'fra', 'deu', 'eng'. "
                "Pushes filter to SQLite — dramatically faster than fetching all and filtering in Python "
                "when the corpus is large (e.g., 1M docs, 22% French → fetch 220K not 1M)."
            ),
        ),
    ] = None,
    category: Annotated[
        Optional[str],
        Field(
            description=(
                "Broad document category to server-side filter by. "
                "One of: 'legal', 'financial', 'hr', 'technical', 'compliance', 'business', 'meeting'. "
                "Pushes filter to SQLite — use instead of fetching all docs and filtering in Python."
            ),
        ),
    ] = None,
    status: Annotated[
        Optional[str],
        Field(
            description=(
                "Document status to filter by: 'ACTIVE', 'PROCESSING', or 'ERROR'. "
                "Omit (null) to return documents of ALL statuses — do not filter by status "
                "unless the user explicitly requested active-only or a specific status."
            ),
        ),
    ] = None,
) -> dict:
    """Get documents in a container with optional server-side filtering and pagination.

    Server-side filter params (language, category, status) push the filter to SQLite —
    critical for 1M+ doc containers where Python-side filtering would load all rows first.

    Pagination for large containers:
      page 1: page_size=10000, page=1  → documents 1–10000
      page 2: page_size=10000, page=2  → documents 10001–20000
      ...until page_info.has_more is false (last page).

    Auto-safety: containers with >50K matching documents are automatically paginated
    (page_size=10000, page=1) when page_size is omitted, preventing out-of-memory errors.

    Returns:
        container_id, total_documents (filtered count), page_info?, and documents list.
        Each document includes ALL pre-computed metadata:
          documentId, documentName, pageCount, size, language, uploadedAt, status,
          category (legal|financial|hr|technical|compliance|business|meeting),
          fileExtension (.pdf|.docx|.xlsx|...),
          classificationCategory, classificationSubcategory, classificationConfidence,
          classificationDocumentType, piiCount, piiTypes, createdAt, updatedAt.
    """
    if not container_id:
        raise ValueError("Container ID is missing.")

    if not _validate_container_exists(container_id):
        available_containers = _get_all_containers()
        raise ValueError(
            f"Container not found: {container_id}. "
            f"Available containers: {', '.join(available_containers)}"
        )

    loop = asyncio.get_running_loop()

    # Filtered count — O(1), pushed to SQLite with index.
    total = await loop.run_in_executor(
        _executor,
        lambda: _count_documents_filtered(container_id, language, category, status),
    )

    # Auto-pagination safety: containers > 50K docs can't be safely fetched without a page_size.
    # Rather than OOM-ing, automatically paginate and signal it clearly in the response.
    auto_paginated = False
    effective_page_size = page_size
    effective_page = page or 1
    if page_size is None and total > _AUTO_PAGINATE_THRESHOLD:
        effective_page_size = 10_000
        effective_page = 1
        auto_paginated = True
        logger.warning(
            f"[get_active_documents_metadata] container {container_id} has {total} matching docs "
            f"(>{_AUTO_PAGINATE_THRESHOLD}); auto-paginating to page_size=10000 page=1 to prevent OOM."
        )

    limit: Optional[int] = None
    offset: Optional[int] = None
    if effective_page_size is not None:
        limit = effective_page_size
        offset = (effective_page - 1) * effective_page_size

    documents = await loop.run_in_executor(
        _executor,
        lambda: _get_documents_by_filters(
            container_id=container_id,
            language=language,
            category=category,
            status=status,
            limit=limit,
            offset=offset,
        ),
    )

    active_filters = {k: v for k, v in {"language": language, "category": category, "status": status}.items() if v}

    logger.info(
        f"[MCP_TOOL_RESULT] get_active_documents_metadata | container: {container_id} | "
        f"filters: {active_filters} | total_filtered: {total} | returned: {len(documents)} | "
        f"page_size: {effective_page_size} | page: {effective_page} | auto_paginated: {auto_paginated}"
    )

    result: dict = {
        "container_id": container_id,
        "documents": documents,
        "total_documents": total,
    }
    if active_filters:
        result["applied_filters"] = active_filters
    if effective_page_size is not None:
        result["page_info"] = {
            "page": effective_page,
            "page_size": effective_page_size,
            "returned": len(documents),
            "has_more": len(documents) == effective_page_size,
        }
    if auto_paginated:
        result["auto_paginated"] = True
        result["auto_paginated_note"] = (
            f"Container has {total} matching documents (>{_AUTO_PAGINATE_THRESHOLD}). "
            f"Returned page 1 of ~{math.ceil(total / 10000)} (10000 docs). "
            f"Set page_size=10000 and iterate pages to retrieve the full set."
        )
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL: translate_document_preserving_structure
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(name="translate_document_preserving_structure")
async def translate_document_preserving_structure(
    document_id: Annotated[
        str | list[str],
        Field(
            description=(
                "The document ID(s) to translate. Can be either:\n"
                "- A single document ID (string): 'doc_001'\n"
                "- A list of document IDs (array): ['doc_001', 'doc_002', 'doc_003']\n\n"
                "For bulk operations, pass a list of document IDs. The tool will translate "
                "all documents and return aggregated results.\n\n"
                "Examples:\n"
                "- Single: 'doc_123456'\n"
                "- Bulk: ['doc_001', 'doc_002', 'doc_003', ..., 'doc_100']"
            ),
        ),
    ],
    destinationLanguageThreeLetterCode: Annotated[
        str,
        Field(
            description=(
                "Target language for translation using ISO 639-3 language codes. "
                "Supported languages include English (eng), Spanish (spa), French (fra), "
                "Italian (ita), Portuguese (por), German (deu), Japanese (jpn), and Chinese (zho). "
                "Format: 3-character lowercase ISO code. Example: 'spa' for Spanish."
            ),
        ),
    ],
    container_id: Annotated[
        str,
        Field(
            description=(
                "The container ID where the document(s) exist. "
                "Examples:\n"
                "- 'container_001'\n"
                "- 'container_004'\n"
            ),
        ),
    ],
) -> dict:
    """
    Translate one or more documents from source language to target language.

    This tool performs machine translation on documents while preserving
    the original formatting, layout, and structure. It supports both single
    document translation and bulk operations.

    **Single Document Mode:**
    Pass a single document ID as a string. Returns translation result for that document.

    **Bulk Mode:**
    Pass a list of document IDs. The tool will iterate through all documents,
    translate each one, and return aggregated results showing success/failure counts.

    Args:
        document_id: Single document ID (string) or list of document IDs (list[str]).
            For bulk operations, pass all document IDs in a list.

        destinationLanguageThreeLetterCode: Target language for translation using ISO 639-3 language codes.
            Supported languages include English (eng), Spanish (spa), French (fra),
            Italian (ita), Portuguese (por), German (deu), Japanese (jpn), and Chinese (zho).
            Format: 3-character lowercase ISO code. Example: 'spa' for Spanish.

    Returns:
        dict: Translation response with different structure depending on mode:

        Single document mode:
        {
            'status': 'success',
            'output_path': 'https://...',
            'message': 'Translation Successful for document doc_123'
        }

        Bulk mode:
        {
            'status': 'success',
            'mode': 'bulk',
            'successful': 95,
            'failed': 5,
            'total': 100,
            'language': 'deu',
            'failed_documents': ['doc_003', 'doc_045', ...],
            'message': 'Bulk translation completed: 95 successful, 5 failed'
        }

    Raises:
        ValueError: If destination language code is invalid
        ValueError: If document_id is empty or invalid
        ValueError: If no container context found
    """
    

    # Determine if this is single or bulk operation
    is_bulk = isinstance(document_id, list)
    doc_count = len(document_id) if is_bulk else 1

    logger.info(
        f"[MCP_TOOL_SELECTED] translate_document_preserving_structure | "
        f"mode: {'bulk' if is_bulk else 'single'} | docs: {doc_count} | "
        f"lang: {destinationLanguageThreeLetterCode} "
    )

    # Validate container exists
    if not container_id:
        raise ValueError("Container ID is missing.")

    if not _validate_container_exists(container_id):
        available_containers = _get_all_containers()
        raise ValueError(
            f"Container not found: {container_id}. "
            f"Available containers: {', '.join(available_containers)}"
        )

    # ═══ SINGLE DOCUMENT MODE ═══
    if not is_bulk:
        if not document_id or not document_id.strip():
            raise ValueError("document_id is required and cannot be empty")

        if not _validate_document_exists(document_id, container_id):
            raise ValueError(
                f"Document not found: {document_id} in container {container_id}. "
            )

        # Non-blocking async sleep — time.sleep() was blocking the event loop.
        await asyncio.sleep(0.05)

        fake_url = (
            f"https://fake-storage.aiclouddrive.example.com/translations/"
            f"{document_id}_translated_{destinationLanguageThreeLetterCode}.pdf"
            f"?expires=1735689600&signature=fake_signature_abc123"
        )
        logger.info(
            f"[MCP_TOOL_RESULT] Translation completed | "
            f"document: {document_id} | lang: {destinationLanguageThreeLetterCode}"
        )
        return {
            "status": "success",
            "output_path": fake_url,
            "message": f"Translation Successful for document {document_id}",
        }

    # ═══ BULK MODE ═══
    logger.info(f"[BULK_MODE] Starting translation of {len(document_id)} documents")

    # ── Batch validation: ONE SQL query instead of N individual DB connections ──
    # Previously: for doc_id in document_id: _validate_document_exists(doc_id)
    # → O(N) connections + queries, ~1ms each → 9s for 9K docs, hours for 1M.
    empty_docs = [d for d in document_id if not d or not str(d).strip()]
    non_empty = [d for d in document_id if d and str(d).strip()]

    valid_ids: set[str] = set()
    if non_empty:
        # Run batch validation in thread pool so we don't block the event loop.
        loop = asyncio.get_running_loop()
        valid_ids = await loop.run_in_executor(
            _executor, _batch_validate_documents, non_empty, container_id
        )

    invalid_docs = [f"{d} (empty)" for d in empty_docs] + [
        d for d in non_empty if d not in valid_ids
    ]

    if invalid_docs:
        invalid_list = (
            ", ".join(invalid_docs[:10])
            + (f" and {len(invalid_docs) - 10} more" if len(invalid_docs) > 10 else "")
        )
        raise ValueError(f"Invalid document IDs found: {invalid_list}.")

    import random

    async def translate_one_doc(doc_id: str, idx: int):
        if not doc_id or not str(doc_id).strip():
            return ("failed", f"index_{idx}_empty", "Empty document ID")
        try:
            # Simulated translation latency — non-blocking.
            await asyncio.sleep(0.02)
            if random.random() < 0.03:
                raise Exception("Simulated translation failure")
            return ("success", doc_id, None)
        except Exception as e:
            logger.warning(f"[BULK_MODE] Translation failed for {doc_id}: {str(e)}")
            return ("failed", doc_id, str(e))

    # Semaphore raised from 20 → 200 for 10× throughput on large corpora.
    # At 0.02s/doc with concurrency=200: 1M docs ≈ 100s instead of 1000s.
    semaphore = asyncio.Semaphore(200)

    async def translate_with_semaphore(doc_id: str, idx: int):
        async with semaphore:
            return await translate_one_doc(doc_id, idx)

    all_tasks = [
        translate_with_semaphore(doc_id, idx)
        for idx, doc_id in enumerate(document_id, 1)
    ]

    # Process in parallel waves; log progress every 1000 docs
    results = []
    wave_size = 1000
    for i in range(0, len(all_tasks), wave_size):
        wave = all_tasks[i : i + wave_size]
        wave_results = await asyncio.gather(*wave)
        results.extend(wave_results)
        processed = min(i + wave_size, len(all_tasks))
        logger.info(f"[BULK_MODE] Progress: {processed}/{len(document_id)} documents processed")

    successful = sum(1 for s, _, __ in results if s == "success")
    failed = len(results) - successful
    failed_docs = [d for s, d, _ in results if s != "success"]

    logger.info(
        f"[MCP_TOOL_RESULT] Bulk translation completed | "
        f"successful: {successful} | failed: {failed} | total: {len(document_id)} | "
        f"lang: {destinationLanguageThreeLetterCode}"
    )
    return {
        "status": "success",
        "mode": "bulk",
        "successful": successful,
        "failed": failed,
        "total": len(document_id),
        "language": destinationLanguageThreeLetterCode,
        "failed_documents": failed_docs[:100],
        "failed_documents_note": (
            f"{len(failed_docs)} total failures" + (
                " — first 100 shown" if len(failed_docs) > 100 else ""
            )
        ),
        "message": (
            f"Bulk translation completed: {successful} successful, "
            f"{failed} failed out of {len(document_id)} documents"
        ),
    }


def _enrich_fts5_candidates_sync(candidates: list[dict]) -> list[dict]:
    """Fetch summary and category from doc_meta for FTS5 results.

    FTS5 only returns content_id, container_id, document_name, bm25_score.
    This adds summary and category so _build_rag_answer can display them.
    """
    if not candidates or not SEARCH_INDEX_PATH.exists():
        return candidates
    cids = [c["content_id"] for c in candidates]
    ph = ",".join("?" * len(cids))
    try:
        idx = sqlite3.connect(str(SEARCH_INDEX_PATH))
        rows = idx.execute(
            f"SELECT content_id, summary, category FROM doc_meta WHERE content_id IN ({ph})",
            cids,
        ).fetchall()
        idx.close()
        meta_map = {r[0]: {"summary": r[1] or "", "category": r[2] or ""} for r in rows}
    except Exception:
        meta_map = {}
    return [
        {
            "content_id": c["content_id"],
            "document_name": c["document_name"],
            "summary": meta_map.get(c["content_id"], {}).get("summary", ""),
            "category": meta_map.get(c["content_id"], {}).get("category", ""),
            # FTS5 bm25() returns negative values; negate for a positive relevance display
            "score": round(abs(c.get("bm25_score", 0.0)), 3),
            "kw_phrases": [],
        }
        for c in candidates
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL: aiagent
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(name="aiagent")
async def aiagent(
    prompt: Annotated[
        str,
        Field(
            description=(
                "User question about the documents stored in the container. "
                "This prompt is forwarded directly to Link (Intralinks AI) which retrieves "
                "and answers using the document corpus."
            ),
        ),
    ],
    container_id: Annotated[
        str,
        Field(
            description=(
                "The container ID where the document(s) exist. "
                "Examples:\n"
                "- 'container_001'\n"
                "- 'container_004'\n"
            ),
        ),
    ],
) -> str:
    """
    AI Agent is LLM chatbot to which you can ask a question about the documents stored in the container.

    This tool performs document-based Question & Answer (RAG) over the documents
    available in the active container. It retrieves relevant information from
    the document corpus and generates an answer based on that content.

    This is the default tool for any document-related question. Use it unless the user explicitly asks to:
    - extract AICloudDrive insights (categories, summary, PIIs) → use "get_document_insights"
    - translate a document → use "translate_document_preserving_structure"

    IMPORTANT: Do NOT ask the user for ANY clarification — not about which document, not about
    what data they need, not about the format, not about anything.
    Just forward the user's question exactly as typed. The tool handles document scoping, content
    retrieval, and answer generation. If a specific document is selected, the answer will be based
    solely on that document. If no specific document is selected, the answer will be based on all
    documents in the container.

    This tool should be used for general document intelligence queries such as:
    - Summarizing documents or specific sections
    - Explaining document contents
    - Extracting information from agreements, contracts, or reports
    - Answering questions about entities, clauses, or risks in documents
    - Comparing documents or finding differences
    - Any free-form question about the document corpus

    Args:
        prompt: The user's question exactly as typed.
                This prompt will be forwarded to Link (Intralinks AI) which
                performs semantic retrieval and generates an answer from
                the document corpus.

                Example:
                "What are the payment terms in the agreement?"
                "Who are the parties mentioned in the contract?"
                "Summarize the key risks mentioned in the document."

    Returns:
        A string response containing the answer generated by Link based on the document corpus.

    Raises:
        ValueError: If the prompt is empty
        ValueError: If required context (appId, containerId) is missing
    """
    


    # Validate required parameters
    if not prompt or prompt.strip() == "":
        raise ValueError("Prompt cannot be empty.")

    if not container_id:
        raise ValueError("ContainerId is required.")

    # Validate container exists
    if not _validate_container_exists(container_id):
        available_containers = _get_all_containers()
        raise ValueError(
            f"Container not found: {container_id}. "
            f"Available containers: {', '.join(available_containers)}"
        )

    loop = asyncio.get_running_loop()

    # Use FTS5 when available (O(log N), scales to 1M+ docs).
    # Fall back to LIKE-based BM25 only when FTS5 is unavailable.
    index_ready = await _ensure_index_built()

    if index_ready:
        fts5_candidates = await loop.run_in_executor(
            _executor, _fts5_search_sync, prompt, container_id, 8
        )
        results = await loop.run_in_executor(
            _executor, _enrich_fts5_candidates_sync, fts5_candidates
        )
        doc_count = await loop.run_in_executor(_executor, _count_documents, container_id)
        retrieval_method = "FTS5"
    else:
        doc_count, results = await loop.run_in_executor(
            _executor, _bm25_search, prompt, container_id, 5
        )
        retrieval_method = "BM25-LIKE"

    logger.info(
        f"[MCP_TOOL] aiagent {retrieval_method} search | container: {container_id} | "
        f"total_docs: {doc_count} | matched: {len(results)}"
    )

    answer = _build_rag_answer(prompt, results, container_id, doc_count)
    answer_with_context = (
        f"[Querying {doc_count} documents in container {container_id} via {retrieval_method}]\n\n"
        f"{answer}"
    )

    logger.info(
        f"[MCP_TOOL_RESULT] AIAgent answer generated | container: {container_id} | "
        f"docs: {doc_count} | matched: {len(results)} | length: {len(answer_with_context)} chars"
    )
    return answer_with_context
