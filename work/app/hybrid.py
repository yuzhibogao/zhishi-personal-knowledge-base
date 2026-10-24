"""Local field-aware keyword/alias search with optional real embedding matches."""
import os
from typing import Optional

from . import db, embedding, vectorstore


def _semantic_hits(conn, query: str, embedder, top_k: int = 50) -> list[dict]:
    """语义路；任何异常（模型未下载、维度不符等）都视为无语义结果。"""
    try:
        emb = embedder or embedding.get_embedder()
        if not vectorstore.has_vectors(conn, emb.name):
            return []
        qvec = emb.embed([query])[0]
        hits = vectorstore.search(conn, qvec, emb.name, top_k)
        # 相似度下限：没有下限时，语义路总会返回 Top-K，把完全不相关的文档混进结果。
        # 阈值与模型相关（Mock 哈希词袋无相关词时余弦为 0；真实 BGE 模型需用 eval_semantic.py 标定），
        # 因此可用 SEM_MIN_SIM 配置。
        # Mock 只有 256 个哈希桶，无关文本撞桶即可产生 0.1~0.2 的余弦，故默认阈值更高
        # 真实模型（bge-small-zh）经 scripts/calibrate_sem_threshold.py 标定：
        # 阈值 0.4 时相关保留 100%、无关误入 4%，故默认 0.4
        default_floor = "0.3" if emb.name.startswith("mock") else "0.4"
        # .env.example 中 SEM_MIN_SIM= 为空串，空串视为未配置，否则 float("") 抛异常被吞，语义路静默失效
        floor = float(os.environ.get("SEM_MIN_SIM", "").strip() or default_floor)
        return [h for h in hits if h["score"] >= floor]
    except Exception:
        return []


def hybrid_search(conn, query: str, limit: int = 10, k: int = 60,
                  source_type=None, time_from=None, time_to=None, embedder=None,
                  category_path=None, tag=None, broad=False, progress=None) -> list[dict]:
    from .concepts import query_groups, contains
    aliases = {}
    aliases_by_doc = {}
    for r in conn.execute("SELECT doc_id,label,alias FROM document_aliases"):
        aliases.setdefault(r["label"], set()).add(r["alias"])
        aliases_by_doc.setdefault(r["doc_id"], []).append(r["alias"])
    aliases={label:sorted(values) for label,values in aliases.items()}
    groups = query_groups(query, aliases)
    if not groups:
        if progress: progress(95,"未识别有效检索词",total=0,processed=0,matched=0,excluded=0)
        return []
    emb = embedder or embedding.get_embedder()
    # Hash vectors are fixtures, not semantic evidence. Never return hash-only matches.
    sem = [] if emb.name.startswith("mock") else _semantic_hits(conn, query, emb, 200)
    semantic = {h["doc_id"]: h for h in sem}
    conds, args = [], []
    if source_type:
        conds.append("d.source_type=?"); args.append(source_type)
    if time_from is not None:
        conds.append("d.created_at>=?"); args.append(time_from)
    if time_to is not None:
        conds.append("d.created_at<=?"); args.append(time_to)
    if category_path:
        conds.append("(d.category_path=? OR substr(d.category_path,1,?)=?)")
        cp = category_path.strip("/")
        args += [cp, len(cp)+1, cp+"/"]
    if tag:
        conds.append("EXISTS(SELECT 1 FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE dt.doc_id=d.id AND t.name=?)")
        args.append(tag)
    where = " WHERE " + " AND ".join(conds) if conds else ""
    rows = conn.execute("SELECT d.*,b.clean_text,s.summary AS cached_summary FROM documents d JOIN document_bodies b ON b.doc_id=d.id LEFT JOIN document_summaries s ON s.doc_id=d.id AND s.content_hash=d.content_hash"+where, args)
    total=conn.execute("SELECT COUNT(*) FROM documents d JOIN document_bodies b ON b.doc_id=d.id"+where,args).fetchone()[0]
    out = []
    if progress:progress(10,"扫描文库",total=total,processed=0,matched=0,excluded=0)
    for index,r in enumerate(rows,1):
        if progress and (index%10==0 or index==1):
            progress(10+int(75*(index-1)/max(total,1)),"扫描文库",processed=index-1,matched=len(out),excluded=index-1-len(out))
        doc_aliases = aliases_by_doc.get(r["id"], [])
        fields = [r["title"] or "", " ".join([r["tags"] or "", *doc_aliases]),
                  r["category_path"] or "", r["clean_text"], r["cached_summary"] or r["summary"] or ""]
        match_groups = [[any(contains(f, alias) for alias in g) for f in fields] for g in groups]
        matched = sum(any(g) for g in match_groups)
        coverage = matched / len(groups)
        semantic_hit = semantic.get(r["id"])
        if not ((coverage > 0 if broad else coverage >= 0.75) or semantic_hit):
            continue
        hits = [a for g in groups for a in g if contains(r["clean_text"], a)]
        text = r["clean_text"]
        positions = [text.casefold().find(a.casefold()) for a in hits]
        pos = min((x for x in positions if x >= 0), default=0)
        start = max(0, pos-70)
        snippet = text[start:start+260]
        if start: snippet = "…" + snippet
        if semantic_hit and not hits: snippet = semantic_hit["text"][:260]
        matched_fields = [name for i, name in enumerate(("标题", "标签/别名", "分类", "正文", "摘要")) if any(g[i] for g in match_groups)]
        score = coverage*5 + sum(g[0]*2 + g[1]*0.8 + g[2]*0.5 + g[4]*0.6 for g in match_groups)/len(groups)
        if semantic_hit: score += semantic_hit["score"]*2
        out.append({"id": r["id"], "title": r["title"], "summary": r["summary"],
                    "source_type": r["source_type"], "status": r["status"],
                    "category_path": r["category_path"], "tags": (r["tags"] or "").split(",") if r["tags"] else [],
                    "score": round(score, 4), "via": (["kw"] if matched else []) + (["sem"] if semantic_hit else []),
                    "snippet": snippet, "matched_terms": hits,
                    "match_reason": f"命中 {matched}/{len(groups)} 个检索概念" + (" · " + "、".join(matched_fields) if matched_fields else "") + (" · 语义相似" if semantic_hit else ""),
                    "partial": coverage < 0.75 and not semantic_hit})
    if progress:progress(90,"排序与生成摘要",processed=total,matched=len(out),excluded=total-len(out))
    out.sort(key=lambda x: (-x["score"], -x["id"]))
    from .summaries import extract
    result=out[:limit]
    for item in result:
        saved=conn.execute("SELECT s.summary FROM document_summaries s JOIN documents d ON d.id=s.doc_id WHERE s.doc_id=? AND s.content_hash=d.content_hash",(item['id'],)).fetchone()
        if saved:item['summary']=saved[0]
        else:
            body=conn.execute('SELECT clean_text FROM document_bodies WHERE doc_id=?',(item['id'],)).fetchone()
            item['summary']=extract(body[0]) if body else ''
    return result
