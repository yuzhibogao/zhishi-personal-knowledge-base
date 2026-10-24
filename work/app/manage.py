"""V0.6 知识管理：文档元数据修正、级联删除、分类树管理。

关键决策：
- 用户修正（分类/标签）写入 corrections 表；同时作为自动分类的反馈信号：见 classify.correction_candidates（相似文档的修正目标会成为候选并置顶）。
- 删除/迁移/合并/改分类时同步维护 categories.doc_count 与 centroid_sum，tags.doc_count，不留孤儿数据。
- 分类树操作违反约束时抛 ValueError / CategoryLimitError，由接口层转成 400。
"""
import time
from typing import Optional

import numpy as np

from . import classify, db


def _model() -> str:
    from . import embedding
    return embedding.get_embedder().name


def _doc_tags(conn, doc_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE dt.doc_id=? ORDER BY t.name",
        (doc_id,))]


def _detach_tags(conn, doc_id: int) -> None:
    for r in conn.execute("SELECT tag_id FROM document_tags WHERE doc_id=?", (doc_id,)).fetchall():
        conn.execute("UPDATE tags SET doc_count=MAX(doc_count-1,0) WHERE id=?", (r[0],))
    conn.execute("DELETE FROM document_tags WHERE doc_id=?", (doc_id,))


def _leave_category(conn, doc_id: int, cat_id: Optional[int], model: str) -> None:
    if cat_id is None:
        return
    classify.remove_from_centroid(conn, cat_id, classify.doc_vector(conn, doc_id, model), model)


def _enter_category(conn, doc_id: int, cat_id: int, model: str) -> None:
    vec = classify.doc_vector(conn, doc_id, model)
    if vec is not None:
        classify.add_to_centroid(conn, cat_id, vec, model)
    else:
        conn.execute("UPDATE categories SET doc_count=doc_count+1 WHERE id=?", (cat_id,))


def update_document(conn, doc_id: int, title: Optional[str] = None,
                    category_path: Optional[str] = None, tags: Optional[list[str]] = None) -> dict:
    """更新标题/分类/标签。返回更新后的元数据；文档不存在返回 None。"""
    d = conn.execute("SELECT id, title, category_id, category_path FROM documents WHERE id=?",
                     (doc_id,)).fetchone()
    if d is None:
        return None
    now, model = time.time(), _model()
    if title is not None:
        title = title.strip()
        if not title:
            raise ValueError("标题不能为空")
        body = conn.execute("SELECT clean_text FROM document_bodies WHERE doc_id=?", (doc_id,)).fetchone()[0]
        conn.execute("UPDATE documents SET title=?, updated_at=? WHERE id=?", (title, now, doc_id))
        conn.execute("DELETE FROM fts_docs WHERE rowid=?", (doc_id,))
        conn.execute("INSERT INTO fts_docs(rowid, title, body) VALUES(?,?,?)",
                     (doc_id, db.seg(title), db.seg(body)))
    if category_path is not None:
        path = "/".join(p.strip() for p in category_path.split("/") if p.strip())
        if not path:
            raise ValueError("分类路径为空")
        if path != d["category_path"]:
            new_id = classify.create_category(conn, path)  # 可能抛 CategoryLimitError
            _leave_category(conn, doc_id, d["category_id"], model)
            _enter_category(conn, doc_id, new_id, model)
            conn.execute("UPDATE documents SET category_id=?, category_path=?, needs_review=0,"
                         " updated_at=? WHERE id=?", (new_id, path, now, doc_id))
            conn.execute("INSERT INTO corrections(doc_id, kind, from_value, to_value, created_at)"
                         " VALUES(?,?,?,?,?)", (doc_id, "category", d["category_path"], path, now))
    if tags is not None:
        names = list(dict.fromkeys(t.strip() for t in tags if t and t.strip()))
        old = _doc_tags(conn, doc_id)
        if sorted(names) != sorted(old):
            _detach_tags(conn, doc_id)
            # Keep aliases only for labels that survived the user's correction.
            for alias_row in conn.execute("SELECT DISTINCT label FROM document_aliases WHERE doc_id=?", (doc_id,)).fetchall():
                if alias_row[0] not in names:
                    conn.execute("DELETE FROM document_aliases WHERE doc_id=? AND label=?", (doc_id, alias_row[0]))
            for n in names:
                conn.execute("INSERT OR IGNORE INTO tags(name, doc_count) VALUES(?,0)", (n,))
                tid = conn.execute("SELECT id FROM tags WHERE name=?", (n,)).fetchone()[0]
                conn.execute("INSERT INTO document_tags(doc_id, tag_id, source) VALUES(?,?,'user')", (doc_id, tid))
                conn.execute("UPDATE tags SET doc_count=doc_count+1 WHERE id=?", (tid,))
            conn.execute("UPDATE documents SET tags=?, updated_at=? WHERE id=?", (",".join(names), now, doc_id))
            conn.execute("INSERT INTO corrections(doc_id, kind, from_value, to_value, created_at)"
                         " VALUES(?,?,?,?,?)", (doc_id, "tags", ",".join(old), ",".join(names), now))
    r = conn.execute("SELECT id, title, category_path, tags FROM documents WHERE id=?", (doc_id,)).fetchone()
    return dict(r)


def delete_document_full(conn, doc_id: int) -> bool:
    """级联删除：切片/向量/正文/FTS/标签关联，并回退分类与标签计数。"""
    d = conn.execute("SELECT category_id FROM documents WHERE id=?", (doc_id,)).fetchone()
    if d is None:
        return False
    _leave_category(conn, doc_id, d["category_id"], _model())
    _detach_tags(conn, doc_id)
    return db.delete_document(conn, doc_id)


# ---------------- 分类树 ----------------

def category_tree(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT c.id,c.name,c.parent_id,c.path,c.depth,c.description,c.is_system,"
        "(SELECT COUNT(*) FROM documents d WHERE d.category_id=c.id) doc_count "
        "FROM categories c ORDER BY c.path").fetchall()
    nodes = {r["id"]: {**dict(r), "children": []} for r in rows}
    roots = []
    for n in nodes.values():
        (nodes[n["parent_id"]]["children"] if n["parent_id"] in nodes else roots).append(n)
    return sorted(roots, key=lambda n: (n["path"] != "未分类", n["path"]))


def _children_count(conn, cat_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM categories WHERE parent_id=?", (cat_id,)).fetchone()[0]


def rename_category(conn, cat_id: int, new_name: str) -> dict:
    new_name = (new_name or "").strip()
    if not new_name or "/" in new_name:
        raise ValueError("分类名不能为空且不能含 '/'")
    c = conn.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
    if c is None:
        return None
    if c["is_system"] or c["path"]=="未分类":
        raise ValueError("未分类及系统分类不可重命名")
    parent = c["path"].rsplit("/", 1)[0] + "/" if "/" in c["path"] else ""
    new_path = parent + new_name
    if new_path != c["path"] and classify.get_by_path(conn, new_path):
        raise ValueError(f"分类已存在：{new_path}")
    old = c["path"]
    now = time.time()
    # 自身与全部后代：路径前缀替换，并同步 documents.category_path
    for r in conn.execute("SELECT id, path FROM categories WHERE path=? OR path LIKE ?",
                          (old, old + "/%")).fetchall():
        p = new_path + r["path"][len(old):]
        conn.execute("UPDATE categories SET path=?, updated_at=? WHERE id=?", (p, now, r["id"]))
        conn.execute("UPDATE documents SET category_path=? WHERE category_id=?", (p, r["id"]))
    conn.execute("UPDATE categories SET name=? WHERE id=?", (new_name, cat_id))
    return {"id": cat_id, "path": new_path}


def merge_category(conn, src_id: int, dst_id: int) -> dict:
    """把 src 的文档并入 dst 后删除 src。src 有子分类时拒绝（需先处理子分类）。"""
    if src_id == dst_id:
        raise ValueError("不能合并到自身")
    src = conn.execute("SELECT * FROM categories WHERE id=?", (src_id,)).fetchone()
    dst = conn.execute("SELECT * FROM categories WHERE id=?", (dst_id,)).fetchone()
    if src is None or dst is None:
        return None
    if src["is_system"]:
        raise ValueError("系统分类不可合并")
    if _children_count(conn, src_id):
        raise ValueError("该分类含子分类，请先合并或删除子分类")
    if dst["path"].startswith(src["path"] + "/"):
        raise ValueError("不能合并到自己的子分类")
    now = time.time()
    moved = conn.execute("UPDATE documents SET category_id=?, category_path=?, updated_at=? WHERE category_id=?",
                         (dst_id, dst["path"], now, src_id)).rowcount
    # 合并 centroid_sum：同模型同维度才累加，否则保持 dst 不变（下次归类时自然重建）
    if (src["centroid_sum"] is not None and dst["centroid_sum"] is not None
            and src["centroid_model"] == dst["centroid_model"] and src["centroid_dim"] == dst["centroid_dim"]):
        total = np.frombuffer(dst["centroid_sum"], dtype=np.float32) + np.frombuffer(src["centroid_sum"], dtype=np.float32)
        norm = np.linalg.norm(total)
        conn.execute("UPDATE categories SET centroid_sum=?, centroid=? WHERE id=?",
                     (total.astype(np.float32).tobytes(),
                      (total / norm if norm else total).astype(np.float32).tobytes(), dst_id))
    elif src["centroid_sum"] is not None and dst["centroid_sum"] is None:
        conn.execute("UPDATE categories SET centroid_sum=?, centroid=?, centroid_dim=?, centroid_model=? WHERE id=?",
                     (src["centroid_sum"], src["centroid"], src["centroid_dim"], src["centroid_model"], dst_id))
    conn.execute("UPDATE categories SET doc_count=doc_count+?, updated_at=? WHERE id=?", (moved, now, dst_id))
    conn.execute("DELETE FROM categories WHERE id=?", (src_id,))
    return {"merged_into": dst["path"], "moved_documents": moved}


def delete_category(conn, cat_id: int, migrate_to=None):
    """Remove a category subtree, keeping all documents in 未分类 atomically."""
    c=conn.execute("SELECT * FROM categories WHERE id=?",(cat_id,)).fetchone()
    if not c:return None
    if c['path']=='未分类':raise ValueError('未分类入口不可删除')
    if migrate_to is not None:raise ValueError('请使用批量归类；删除分类会将资料移入未分类')
    rows=conn.execute("SELECT id,depth FROM categories WHERE path=? OR substr(path,1,?)=? ORDER BY depth DESC",(c['path'],len(c['path'])+1,c['path']+'/')).fetchall()
    ids=[r['id'] for r in rows];marks=','.join('?' for _ in ids)
    target=classify.create_category(conn,'未分类',is_system=1)
    doc_ids=[r[0] for r in conn.execute(f'SELECT id FROM documents WHERE category_id IN ({marks})',ids)]
    for doc_id in doc_ids:
        conn.execute("DELETE FROM corrections WHERE doc_id=? AND kind='category'",(doc_id,))
        conn.execute("UPDATE documents SET category_id=?,category_path='未分类',updated_at=? WHERE id=?",(target,time.time(),doc_id))
    for row in rows:conn.execute('DELETE FROM categories WHERE id=?',(row['id'],))
    rebuild_centroid(conn,target,_model())
    return {'deleted':c['path'],'categories_deleted':len(rows),'moved_documents':len(doc_ids),'destination':'未分类'}


def list_tags(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, doc_count FROM tags WHERE doc_count>0 ORDER BY doc_count DESC, name")]


def rebuild_centroid(conn, cat_id, model):
    vectors=[]
    ids=[r[0] for r in conn.execute("SELECT id FROM documents WHERE category_id=?",(cat_id,))]
    for doc_id in ids:
        vector=classify.doc_vector(conn,doc_id,model)
        if vector is not None: vectors.append(vector)
    if not vectors:
        conn.execute("UPDATE categories SET centroid=NULL,centroid_sum=NULL,doc_count=? WHERE id=?",(len(ids),cat_id))
        return
    total=np.sum(vectors,axis=0).astype(np.float32)
    norm=float(np.linalg.norm(total))
    centroid=total/norm if norm else total
    conn.execute("UPDATE categories SET centroid=?,centroid_sum=?,centroid_dim=?,centroid_model=?,doc_count=? WHERE id=?",(centroid.tobytes(),total.tobytes(),len(total),model,len(ids),cat_id))
