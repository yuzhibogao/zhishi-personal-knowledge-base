"""V0.3 处理流水线骨架：状态机 pending -> processing -> stored / failed。

关键决策：
- 数据先行：文档在进入本模块之前已经落库（正文 + FTS），本模块任何失败都只改 status/error_message，
  绝不删除或改写正文，保证"LLM 全挂时文档仍入库且可关键词检索"。
- 每一步独立提交：摘要成功即写入，后续步骤失败不会回滚已完成的步骤。
- 可重试：failed / processing（进程异常中断）的文档可再次处理；重试只重做缺失的步骤。
- LLM 通过 Provider 抽象注入，默认 Mock（无需 key、无需联网），便于测试注入故障。
"""
import json
import os
import time
from typing import Optional

from . import db, locks, runtime


class LLMProvider:
    """LLM 抽象接口，后续实现 DeepSeek / Ollama 只需实现 summarize。"""

    def summarize(self, text: str) -> dict:
        raise NotImplementedError

    def tag(self, text: str) -> list[str]:
        """返回 3~8 个短名词标签。"""
        raise NotImplementedError

    def tag_with_aliases(self, text: str):
        from .concepts import CONCEPTS
        names = self.tag(text)
        aliases = {label: terms for label, _, terms in CONCEPTS if label in names}
        return names, aliases

    def classify(self, text: str, summary: str, candidates: list[dict]) -> dict:
        """在候选分类内定夺或新建，返回 {"action": "assign|create", "category_path", "new_category"}。"""
        raise NotImplementedError


class MockLLM(LLMProvider):
    """确定性 Mock：摘要取开头 100 字，要点取前 3 个非空行。fail=True 时模拟 LLM 报错。"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def summarize(self, text: str) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("Mock LLM 故障（模拟无网/超时）")
        from .summaries import extract
        summary=extract(text)
        return {"summary":summary,"key_points":[summary]}

    def tag(self, text: str) -> list[str]:
        """确定性 Mock：取出现频次最高的 >=2 字词（频次相同按首次出现顺序），最多 5 个。"""
        self.calls += 1
        if self.fail:
            raise RuntimeError("Mock LLM 故障（模拟无网/超时）")
        from .concepts import tags
        return tags(text)

    def classify(self, text: str, summary: str, candidates: list[dict]) -> dict:
        """确定性 Mock：候选分类的末级名称出现在正文中则归入该候选，否则新建『通用/<首个高频词>』。"""
        self.calls += 1
        if self.fail:
            raise RuntimeError("Mock LLM 故障（模拟无网/超时）")
        from .concepts import local_category
        return {"action":"create","new_category":local_category(text)}


_provider: Optional[LLMProvider] = None


def get_llm() -> LLMProvider:
    """按 LLM_PROVIDER 选择实现；mock 或真实 Provider（deepseek/openai/ollama）。"""
    global _provider
    if _provider is None:
        name = os.environ.get("LLM_PROVIDER", "mock").lower()
        if name == "mock":
            _provider = MockLLM()
        else:
            # 真实 Provider 惰性导入，避免循环依赖；构造失败（如缺 key）会抛异常，由 pipeline 转 failed
            from . import llm
            _provider = llm.build_llm(name)
    return _provider


def set_llm(provider: Optional[LLMProvider]) -> None:
    """测试用：注入或重置 Provider。"""
    global _provider
    _provider = provider


def _set_status(conn, doc_id: int, status: str, error: Optional[str] = None) -> None:
    conn.execute("UPDATE documents SET status=?, error_message=?, updated_at=? WHERE id=?",
                 (status, error, time.time(), doc_id))
    conn.commit()


@locks.processing
def process_document(conn, doc_id: int, llm: Optional[LLMProvider] = None,
                     embedder=None) -> str:
    """处理单个文档，返回最终状态。永不抛异常，失败记入 error_message。"""
    row = conn.execute(
        "SELECT d.summary, d.category_id, b.clean_text FROM documents d "
        "JOIN document_bodies b ON b.doc_id=d.id WHERE d.id=?", (doc_id,)).fetchone()
    if row is None:
        return "missing"
    _set_status(conn, doc_id, "processing")
    try:
        llm = llm or runtime.provider(conn, doc_id)
        # Combine metadata and category decisions only when the whole taxonomy fits.
        # The same full prompt rules and text sample are retained; manual metadata is untouched.
        if not row['summary'] and row['category_id'] is None and hasattr(llm,'prepare_document'):
            count=conn.execute("SELECT COUNT(*) FROM categories WHERE path!='未分类'").fetchone()[0]
            if count<=80:
                taxonomy=[dict(r) for r in conn.execute("SELECT path,description FROM categories WHERE path!='未分类' ORDER BY path")]
                for c in taxonomy:c['description']=(c['description'] or '')[:180]
                llm.prepare_document(row['clean_text'],taxonomy)

        if not row["summary"]:  # 重试时跳过已完成步骤
            out = llm.summarize(row["clean_text"])
            from .summaries import finalize
            out["summary"]=finalize(out["summary"],row["clean_text"])
            conn.execute("INSERT OR REPLACE INTO document_summaries(doc_id,content_hash,summary) SELECT id,content_hash,? FROM documents WHERE id=?",(out["summary"],doc_id))
            conn.execute("UPDATE documents SET summary=?, key_points=?, updated_at=? WHERE id=?",
                         (out["summary"], json.dumps(out["key_points"], ensure_ascii=False),
                          time.time(), doc_id))
            conn.commit()
        # V0.4：标签。已有 llm 标签则跳过（重试只重做缺失步骤），每步独立提交
        has_tags = conn.execute(
            "SELECT 1 FROM document_tags WHERE doc_id=? LIMIT 1",
            (doc_id,)).fetchone()
        manual_tags = conn.execute("SELECT 1 FROM corrections WHERE doc_id=? AND kind='tags' LIMIT 1", (doc_id,)).fetchone()
        if not has_tags and not manual_tags:
            names = []
            if hasattr(llm, "tag_with_aliases"):
                generated, aliases = llm.tag_with_aliases(row["clean_text"])
            else:
                generated, aliases = llm.tag(row["clean_text"]), {}
            for n in generated:
                n = (n or "").strip()
                if n and n not in names:
                    names.append(n)
            conn.execute("DELETE FROM document_aliases WHERE doc_id=?", (doc_id,))
            for label, terms in aliases.items():
                if label in names:
                    for term in terms[:8]:
                        conn.execute("INSERT OR IGNORE INTO document_aliases(doc_id,label,alias) VALUES(?,?,?)",
                                     (doc_id, label, str(term).strip()[:100]))
            for n in names:
                conn.execute("INSERT OR IGNORE INTO tags(name, doc_count) VALUES(?,0)", (n,))
                tid = conn.execute("SELECT id FROM tags WHERE name=?", (n,)).fetchone()[0]
                cur = conn.execute(
                    "INSERT OR IGNORE INTO document_tags(doc_id, tag_id, source) VALUES(?,?,'llm')",
                    (doc_id, tid))
                if cur.rowcount:
                    conn.execute("UPDATE tags SET doc_count=doc_count+1 WHERE id=?", (tid,))
            conn.execute("UPDATE documents SET tags=?, updated_at=? WHERE id=?",
                         (",".join(names), time.time(), doc_id))
            conn.commit()
        # V0.4：切片 + 向量化。已有同模型向量则跳过（重试只重做缺失步骤）
        from . import embedding, parsers, vectorstore
        emb = embedder or embedding.get_embedder()
        done = conn.execute(
            "SELECT 1 FROM chunks WHERE doc_id=? AND embed_model=? LIMIT 1",
            (doc_id, emb.name)).fetchone()
        if not done:
            chunks = parsers.split_chunks(row["clean_text"])
            vecs = emb.embed([c[2] for c in chunks])
            vectorstore.upsert(conn, doc_id, chunks, vecs, emb.name)
            conn.commit()
        # V0.5：两段式分类（向量筛候选 + LLM 定夺）。已有分类则跳过（重试只重做缺失步骤）
        from . import classify
        before=conn.execute("SELECT category_id,summary FROM documents WHERE id=?",(doc_id,)).fetchone()
        prepared=None
        dvec=None
        if before and before['category_id'] is None:
            dvec=classify.doc_vector(conn,doc_id,emb.name)
            prepared=classify.prepare_decision(conn,doc_id,row['clean_text'],before['summary'] or '',llm,dvec,emb.name)
        with locks.catalog:
            cur = conn.execute("SELECT category_id, summary FROM documents WHERE id=?", (doc_id,)).fetchone()
            if cur["category_id"] is None:
                from . import classify
                dvec = classify.doc_vector(conn, doc_id, emb.name)
                classify.classify_document(conn, doc_id, row["clean_text"], cur["summary"] or "",
                                           llm, dvec, emb.name, prepared=prepared)
                conn.commit()
            from . import manage
            category=conn.execute("SELECT category_id FROM documents WHERE id=?",(doc_id,)).fetchone()
            if category and category[0]: manage.rebuild_centroid(conn,category[0],emb.name)
        _set_status(conn, doc_id, "stored")
        return "stored"
    except Exception as e:  # 任何失败都保留文档
        conn.rollback()
        _set_status(conn, doc_id, "failed", f"{type(e).__name__}: {e}")
        return "failed"


def reset_generated(conn, doc_id, commit=True):
    """Explicit refresh: preserve manual labels/categories, rebuild automatic metadata/vectors."""
    from . import manage, embedding
    conn.execute("DELETE FROM document_summaries WHERE doc_id=?",(doc_id,))
    manual = {r[0] for r in conn.execute("SELECT kind FROM corrections WHERE doc_id=?", (doc_id,))}
    if "tags" not in manual:
        manage._detach_tags(conn, doc_id)
        conn.execute("DELETE FROM document_aliases WHERE doc_id=?", (doc_id,))
        conn.execute("UPDATE documents SET tags=NULL WHERE id=?", (doc_id,))
    if "category" not in manual:
        row = conn.execute("SELECT category_id FROM documents WHERE id=?", (doc_id,)).fetchone()
        manage._leave_category(conn, doc_id, row[0], embedding.get_embedder().name)
        conn.execute("UPDATE documents SET category_id=NULL,category_path=NULL WHERE id=?", (doc_id,))
    conn.execute("UPDATE documents SET summary=NULL,key_points=NULL,status='pending' WHERE id=?", (doc_id,))
    # Body has not changed. Existing vectors remain valid; a changed embedding model
    # is detected by process_document and automatically rebuilds the index.
    if commit: conn.commit()


def recover(db_path: str, llm: Optional[LLMProvider] = None) -> list[int]:
    """进程重启恢复：重新投递 pending / processing 的遗留文档，返回被处理的 id。"""
    with db.session(db_path) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM documents WHERE status IN ('pending','processing') ORDER BY id")]
        for i in ids:
            process_document(conn, i, llm)
    return ids


def process_by_id(db_path: str, doc_id: int, llm: Optional[LLMProvider] = None) -> str:
    with db.session(db_path) as conn:
        return process_document(conn, doc_id, llm)
