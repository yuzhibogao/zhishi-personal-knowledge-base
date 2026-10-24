"""V0.1 数据库层：SQLite + FTS5（jieba 预分词）。

关键决策：
- FTS5 默认 unicode61 不能切中文，trigram 要求查询词 >= 3 字符（已实测 2 字词返回 0 条），
  因此入库与查询共用同一分词器 jieba，空格拼接后写入 FTS5，粒度天然对齐。
- 数据先行：文档先以 status=pending 落库，后续处理失败也不丢正文。
"""
import hashlib
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import jieba

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'paste',
    source_platform TEXT, source_url TEXT, author TEXT, published_at TEXT,
    summary TEXT, key_points TEXT,
    category_id INTEGER, category_path TEXT, tags TEXT,
    locator TEXT,
    conversation_id TEXT, conversation_title TEXT, turn_start INTEGER, turn_end INTEGER,
    content_hash TEXT, char_count INTEGER DEFAULT 0, lang TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    needs_review INTEGER DEFAULT 0, error_message TEXT,
    captured_at REAL, created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    path TEXT NOT NULL UNIQUE,
    depth INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    centroid BLOB, centroid_dim INTEGER, centroid_model TEXT, centroid_sum BLOB,
    doc_count INTEGER DEFAULT 0,
    is_system INTEGER DEFAULT 0,
    created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id);
CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category_id);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER, kind TEXT NOT NULL,
    from_value TEXT, to_value TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    doc_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS document_tags (
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'llm',
    PRIMARY KEY (doc_id, tag_id)
);
CREATE TABLE IF NOT EXISTS document_bodies (
    doc_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    clean_text TEXT NOT NULL, raw_text TEXT,
    compress_algo TEXT DEFAULT 'none', compressed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    char_start INTEGER, char_end INTEGER,
    turn_start INTEGER, turn_end INTEGER,
    embedding BLOB, embed_dim INTEGER, embed_model TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_model ON chunks(embed_model);
CREATE TABLE IF NOT EXISTS originals (
    doc_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    filename TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL, data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS document_aliases (
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    label TEXT NOT NULL, alias TEXT NOT NULL,
    PRIMARY KEY(doc_id,label,alias)
);
CREATE TABLE IF NOT EXISTS document_summaries (
    doc_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS document_processing (
    doc_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    mode TEXT NOT NULL DEFAULT 'offline'
);
CREATE INDEX IF NOT EXISTS idx_originals_sha256 ON originals(sha256);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs USING fts5(
    title, body, tokenize='unicode61'
);
"""

_WORD = re.compile(r"[\w]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """jieba 搜索模式分词，丢弃纯标点/空白；入库与查询共用。"""
    return [t.strip() for t in jieba.cut_for_search(text) if _WORD.fullmatch(t.strip())]


def seg(text: str) -> str:
    return " ".join(tokenize(text))


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(db_path: str):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def default_title(text: str, limit: int = 30) -> str:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "无标题")
    return first[:limit]


def add_document(conn, text: str, title: str | None = None,
                 source_type: str = "paste", source_platform: str | None = None) -> int:
    """先落库(status=pending)，同时写入 FTS5。正文为空则报错，不静默存空。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("正文为空，拒绝入库")
    now = time.time()
    title = (title or "").strip() or default_title(text)
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cur = conn.execute(
        "INSERT INTO documents(title, source_type, source_platform, content_hash, char_count,"
        " status, captured_at, created_at, updated_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
        (title, source_type, source_platform, h, len(text), now, now, now))
    doc_id = cur.lastrowid
    conn.execute("INSERT INTO document_bodies(doc_id, clean_text, raw_text) VALUES(?,?,?)",
                 (doc_id, text, text))
    conn.execute("INSERT INTO fts_docs(rowid, title, body) VALUES(?,?,?)",
                 (doc_id, seg(title), seg(text)))
    return doc_id


def find_duplicate(conn, text: str, exclude_id=None) -> int | None:
    """按 content_hash 查找已有的相同正文，返回最早的文档 id，没有则返回 None。
    设计理由：重复导入时不覆盖/合并已有文档，避免冲掉用户已手工修正的分类与标签；
    只把重复情况告知调用方，是否保留由用户决定。"""
    h = hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()
    r = conn.execute("SELECT MIN(id) FROM documents WHERE content_hash=? AND id!=?", (h,exclude_id or -1)).fetchone()
    if r and r[0] is not None:
        return r[0]
    from .dedup import normalized_text
    canonical = normalized_text(text)
    if not canonical:
        return None
    for existing in conn.execute("SELECT doc_id,clean_text FROM document_bodies WHERE doc_id!=? ORDER BY doc_id",(exclude_id or -1,)):
        if normalized_text(existing["clean_text"]) == canonical:
            return existing["doc_id"]
    return None


def delete_document(conn, doc_id: int) -> bool:
    """级联清理：正文由外键删除，FTS 需手动删除，避免孤儿索引。"""
    conn.execute("DELETE FROM fts_docs WHERE rowid=?", (doc_id,))
    cur = conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    return cur.rowcount > 0


def _fts_query(terms: list[str], op: str) -> str:
    # 每个词加引号，避免 FTS5 把特殊字符当语法
    return f" {op} ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _rank_ids(conn, match: str, limit: int) -> list[int]:
    try:
        rows = conn.execute(
            "SELECT rowid FROM fts_docs WHERE fts_docs MATCH ? ORDER BY bm25(fts_docs) LIMIT ?",
            (match, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows]


def search(conn, query: str, limit: int = 10, k: int = 60) -> list[dict]:
    """关键词检索：AND 档 + OR 档两路 -> RRF 融合（只用排名，不用 BM25 分数）。"""
    terms = list(dict.fromkeys(tokenize(query)))  # 去重保序
    if not terms:
        return []
    lists = [_rank_ids(conn, _fts_query(terms, "AND"), 50)]
    if len(terms) > 1:
        lists.append(_rank_ids(conn, _fts_query(terms, "OR"), 50))
    scores: dict[int, float] = {}
    for ids in lists:
        for rank, d in enumerate(ids, start=1):
            scores[d] = scores.get(d, 0.0) + 1.0 / (k + rank)
    top = sorted(scores, key=scores.get, reverse=True)[:limit]
    out = []
    for d in top:
        r = conn.execute(
            "SELECT d.id, d.title, d.source_type, d.status, d.char_count, b.clean_text "
            "FROM documents d JOIN document_bodies b ON b.doc_id=d.id WHERE d.id=?", (d,)).fetchone()
        if r:
            out.append({"id": r["id"], "title": r["title"], "source_type": r["source_type"],
                        "status": r["status"], "score": round(scores[d], 6),
                        "snippet": make_snippet(r["clean_text"], terms)})
    return out


def make_snippet(text: str, terms: list[str], width: int = 60) -> str:
    """命中片段：定位第一个命中词，并用【】高亮所有命中词。"""
    pos = -1
    for t in terms:
        pos = text.find(t)
        if pos >= 0:
            break
    start = max(0, pos - width) if pos >= 0 else 0
    frag = text[start:start + 2 * width]
    for t in sorted(terms, key=len, reverse=True):
        frag = frag.replace(t, f"【{t}】")
    return ("…" if start > 0 else "") + frag
