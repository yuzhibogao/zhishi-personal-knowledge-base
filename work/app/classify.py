"""V0.5 两段式分类：向量相似度筛 Top-K 候选分类 -> LLM 在候选内定夺（或新建）。

关键决策：
- 分类体系非预定义，随内容生长；优先归入已有分类，都不合适才新建。
- 约束：深度上限 3，单节点子分类上限 12；超限时不新建，退回最相近的已有分类并标记 needs_review。
- 冷启动（无任何候选）：直接让 LLM 创建分类。
- centroid = 该分类下所有文档向量均值（文档向量取其切片向量均值，再归一化），增量维护：
  新增/移除文档时按 doc_count 做加/减，避免每次全量重算。
- 候选筛选只用 centroid，不把全部分类塞进 prompt。
"""
import time
from typing import Optional

import numpy as np

MAX_DEPTH = 3


def normalize_path(path: str) -> str:
    """规范化 LLM 返回的分类路径：去首尾空白与标点（如全角冒号）、统一斜杠、去掉空段。
    设计理由：LLM 偶尔输出 '生活/烹饪：' 这类带尾部标点的路径，若不规范化会被当成新分类，造成分类膨胀。"""
    import re
    p = (path or "").replace("\\", "/").replace("／", "/")
    parts = []
    for seg in p.split("/"):
        seg = seg.strip().strip("：:；;，,。.、\"'“”‘’ \t")
        if seg:
            parts.append(seg)
    return "/".join(parts)
MAX_CHILDREN = 12
TOP_K = 10


class CategoryLimitError(ValueError):
    """分类树约束被突破。"""


def _vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


def _norm(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return (v / n).astype(np.float32) if n else v.astype(np.float32)


def doc_vector(conn, doc_id: int, model: str) -> Optional[np.ndarray]:
    """文档向量 = 其全部切片向量均值（float16 存储，读出转 float32）。"""
    rows = conn.execute(
        "SELECT embedding FROM chunks WHERE doc_id=? AND embed_model=?", (doc_id, model)).fetchall()
    if not rows:
        return None
    m = np.vstack([np.frombuffer(r[0], dtype=np.float16).astype(np.float32) for r in rows])
    return _norm(m.mean(axis=0))


def get_by_path(conn, path: str):
    return conn.execute("SELECT * FROM categories WHERE path=?", (path,)).fetchone()


def create_category(conn, path: str, description: str = "", is_system: int = 0) -> int:
    """按物化路径创建（如 '技术/数据库'）；父级不存在则逐级创建。校验深度与子节点数。"""
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        raise ValueError("分类路径为空")
    if len(parts) > MAX_DEPTH:
        raise CategoryLimitError(f"分类深度超过上限 {MAX_DEPTH}：{path}")
    now = time.time()
    parent_id, cur_path, cid = None, "", None
    for depth, name in enumerate(parts, start=1):
        cur_path = f"{cur_path}/{name}" if cur_path else name
        row = get_by_path(conn, cur_path)
        if row:
            parent_id, cid = row["id"], row["id"]
            continue
        if parent_id is not None:
            n = conn.execute("SELECT COUNT(*) FROM categories WHERE parent_id=?", (parent_id,)).fetchone()[0]
            if n >= MAX_CHILDREN:
                raise CategoryLimitError(f"『{cur_path.rsplit('/', 1)[0]}』子分类已达上限 {MAX_CHILDREN}，请合并")
        # 并发安全：多个后台线程可能同时新建同一分类（先查后插存在竞态），
        # 撞上 path 唯一约束时视为"已被他人创建"，回查已有行即可，不让整篇文档失败。
        import sqlite3
        try:
            cur = conn.execute(
                "INSERT INTO categories(name, parent_id, path, depth, description, is_system,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (name, parent_id, cur_path, depth, description if depth == len(parts) else "",
                 is_system, now, now))
            parent_id = cid = cur.lastrowid
        except sqlite3.IntegrityError:
            row = get_by_path(conn, cur_path)
            if not row:
                raise
            parent_id = cid = row["id"]
    return cid


def add_to_centroid(conn, cat_id: int, vec: np.ndarray, model: str) -> None:
    """增量加入：new = (old * n + v) / (n + 1)，再归一化。换模型则重置。"""
    row = conn.execute("SELECT centroid, centroid_sum, centroid_dim, centroid_model, doc_count "
                       "FROM categories WHERE id=?", (cat_id,)).fetchone()
    n = row["doc_count"] or 0
    if (row["centroid_sum"] is None or row["centroid_model"] != model
            or row["centroid_dim"] != len(vec)):
        total, n = vec.astype(np.float32), 0
    else:
        total = _vec(row["centroid_sum"]) + vec
    new = _norm(total)  # centroid 只是归一化视图；未归一化的和另存，保证可精确移除
    conn.execute("UPDATE categories SET centroid=?, centroid_sum=?, centroid_dim=?, centroid_model=?,"
                 " doc_count=?, updated_at=? WHERE id=?",
                 (new.tobytes(), total.astype(np.float32).tobytes(), len(new), model, n + 1,
                  time.time(), cat_id))


def remove_from_centroid(conn, cat_id: int, vec: Optional[np.ndarray], model: str) -> None:
    """增量移除：new = (old * n - v) / (n - 1)；归零则清空 centroid。"""
    row = conn.execute("SELECT centroid, centroid_sum, centroid_dim, centroid_model, doc_count "
                       "FROM categories WHERE id=?", (cat_id,)).fetchone()
    if row is None:
        return
    n = max((row["doc_count"] or 0), 0)
    if n <= 1:  # 最后一篇：清空 centroid 与累加和
        conn.execute("UPDATE categories SET doc_count=?, centroid=NULL, centroid_sum=NULL,"
                     " updated_at=? WHERE id=?", (max(n - 1, 0), time.time(), cat_id))
        return
    if vec is None or row["centroid_sum"] is None or row["centroid_model"] != model \
            or row["centroid_dim"] != len(vec):
        # 无法精确还原：只减计数，保留现有 centroid
        conn.execute("UPDATE categories SET doc_count=?, updated_at=? WHERE id=?",
                     (n - 1, time.time(), cat_id))
        return
    total = _vec(row["centroid_sum"]) - vec.astype(np.float32)
    conn.execute("UPDATE categories SET centroid=?, centroid_sum=?, doc_count=?, updated_at=? WHERE id=?",
                 (_norm(total).tobytes(), total.astype(np.float32).tobytes(), n - 1,
                  time.time(), cat_id))


def candidates(conn, vec: np.ndarray, model: str, top_k: int = TOP_K) -> list[dict]:
    """第一段：按 centroid 余弦相似度取 Top-K 候选（只看同模型、有 centroid 的分类）。"""
    rows = conn.execute(
        "SELECT id, path, description, centroid, centroid_dim FROM categories "
        "WHERE centroid IS NOT NULL AND centroid_model=?", (model,)).fetchall()
    scored = []
    for r in rows:
        if r["centroid_dim"] != len(vec):
            continue
        scored.append({"id": r["id"], "path": r["path"], "description": r["description"],
                       "score": float(_vec(r["centroid"]) @ vec)})
    scored.sort(key=lambda c: -c["score"])
    return scored[:top_k]


def correction_candidates(conn, vec: np.ndarray, model: str, top_k: int = 3) -> list[dict]:
    """用户修正反馈：找出"用户手动改过分类、且与当前文档相似"的文档，返回其目标分类。

    关键决策：
    - 只取每篇文档最近一次分类修正，且该修正仍然生效（文档当前分类 == 修正目标），
      避免用户后来又改走的旧修正继续干扰。
    - 相似度阈值 CORR_MIN_SIM 可配置（默认 0.5）；不足阈值则不引入，宁缺毋滥。
    - 只作为候选加权提示，最终仍由 LLM 在候选内定夺，不强制覆盖。
    """
    import os
    floor = float(os.environ.get("CORR_MIN_SIM", "0.5"))
    rows = conn.execute(
        "SELECT c.doc_id, c.to_value FROM corrections c "
        "JOIN documents d ON d.id=c.doc_id AND d.category_path=c.to_value "
        "WHERE c.kind='category' AND c.id=(SELECT MAX(id) FROM corrections "
        "WHERE doc_id=c.doc_id AND kind='category')").fetchall()
    best: dict[str, float] = {}
    for r in rows:
        dv = doc_vector(conn, r["doc_id"], model)
        if dv is None or len(dv) != len(vec):
            continue
        s = float(dv @ vec)
        if s >= floor and s > best.get(r["to_value"], -1):
            best[r["to_value"]] = s
    out = []
    for path, s in sorted(best.items(), key=lambda kv: -kv[1])[:top_k]:
        cat = get_by_path(conn, path)
        if cat:
            out.append({"id": cat["id"], "path": path, "description": cat["description"],
                        "score": s, "user_corrected": True})
    return out


def _fallback(cands: list[dict]) -> Optional[dict]:
    return cands[0] if cands else None


def prepare_decision(conn, doc_id, text, summary, llm, vec, model):
    """Read candidate snapshot and request classification without the catalog write lock."""
    # Mock hash vectors are not semantic similarity and must not restrict an
    # external language model to accidentally matching broad categories.
    cands = candidates(conn, vec, model) if vec is not None and not model.startswith("mock") else []
    if vec is not None and not model.startswith("mock"):
        # 用户修正反馈：把"相似文档被用户改过的目标分类"置顶并去重，最终仍由 LLM 定夺
        corr = correction_candidates(conn, vec, model)
        if corr:
            seen = {c["path"] for c in corr}
            cands = corr + [c for c in cands if c["path"] not in seen]
    # Offer the actual taxonomy too, ranked by explicit subject overlap.
    # For a small library this includes every category; cap large taxonomies.
    from .db import tokenize
    terms=set(tokenize(summary+" "+text[:6000]))
    all_categories=[dict(r) for r in conn.execute("SELECT id,path,description FROM categories WHERE path!='未分类'")]
    def overlap(c):
        return len(terms & set(tokenize(c['path']+' '+(c['description'] or ''))))
    all_categories.sort(key=lambda c:(-overlap(c),c['path']))
    seen={c['path'] for c in cands}
    cands += [c for c in all_categories if c['path'] not in seen]
    cands=cands[:80]
    for c in cands:c['description']=(c.get('description') or '')[:180]
    decision = llm.classify(text, summary, [{"path": c["path"], "description": c["description"]}
                                            for c in cands])
    return decision, [c['path'] for c in cands]


def classify_document(conn, doc_id: int, text: str, summary: str, llm, vec: Optional[np.ndarray],
                      model: str, prepared=None) -> dict:
    """两段式分类并落库。返回 {category_path, action, needs_review}。

    LLM 决策 JSON：{"action":"assign|create","category_path":"...","new_category":"..."}。
    约束被突破时退回最相近候选并置 needs_review=1，不抛异常、不丢数据。
    """
    decision, snapshot_paths = prepared if prepared is not None else prepare_decision(conn,doc_id,text,summary,llm,vec,model)
    # A category deleted while the provider was running must not be recreated.
    path=normalize_path(str(decision.get('category_path') or decision.get('new_category') or ''))
    prefixes={'/'.join(path.split('/')[:i]) for i in range(1,len(path.split('/'))+1)}
    if any(p in prefixes and not get_by_path(conn,p) for p in snapshot_paths):
        decision={'action':'create','new_category':'未分类'}
    action = decision.get("action", "assign")
    # 规范化 LLM 返回的路径：去掉首尾空白、全/半角冒号句号等标点与多余斜杠，
    # 否则 "生活/烹饪：" 会被当成新分类，造成过度新增（真实模型评估中发现）
    decision = dict(decision)
    for k in ("category_path", "new_category"):
        if decision.get(k):
            decision[k] = normalize_path(str(decision[k]))
    needs_review = 0
    cat_id, path = None, None
    if action == "assign" and decision.get("category_path") and get_by_path(conn, decision["category_path"]):
        row = get_by_path(conn, decision["category_path"])
        cat_id, path = row["id"], row["path"]
    else:
        new_path = decision.get("new_category") or decision.get("category_path")
        try:
            if not new_path:
                raise ValueError("LLM 未给出分类")
            cat_id = create_category(conn, new_path)
            path = new_path
        except (CategoryLimitError, ValueError):
            fb = None  # No supported decision: leave for review instead of guessing.
            if fb is None:  # 冷启动且无法新建：落到系统兜底分类
                cat_id = create_category(conn, "未分类", is_system=1)
                path = "未分类"
            else:
                cat_id, path = fb["id"], fb["path"]
            needs_review = 1
    if path == "未分类":
        needs_review = 1
    # 幂等：LLM 调用期间文档可能已被另一处理线程/重启恢复流程归类，
    # 此时先从旧分类退出，避免 doc_count 与 centroid 重复累计
    prev = conn.execute("SELECT category_id FROM documents WHERE id=?", (doc_id,)).fetchone()
    if prev and prev["category_id"] is not None:
        remove_from_centroid(conn, prev["category_id"], vec, model)
    if vec is not None:
        add_to_centroid(conn, cat_id, vec, model)
    else:
        conn.execute("UPDATE categories SET doc_count=doc_count+1 WHERE id=?", (cat_id,))
    conn.execute("UPDATE documents SET category_id=?, category_path=?, needs_review=?, updated_at=? WHERE id=?",
                 (cat_id, path, needs_review, time.time(), doc_id))
    return {"category_path": path, "action": action, "needs_review": needs_review}
