"""V0.4 向量存储：float16 BLOB + numpy 暴力余弦（统一接口 upsert/search/delete）。

关键决策：
- 与 SQLite 同文件，chunks 表（建表在 db.SCHEMA）；外键 ON DELETE CASCADE，删文档自动清切片向量。
- 个人规模（<=5 万切片）暴力余弦亚秒级足够；后续可换 sqlite-vec 而上层不感知。
- 只比较同一 embed_model 的向量，避免换模型后维度/空间混用。
- 向量以 float16 存储，读出转 float32 计算。
"""
import numpy as np


def upsert(conn, doc_id: int, chunks: list[tuple[int, int, str]], vecs: np.ndarray,
           model: str) -> int:
    """替换某文档的全部切片与向量。chunks: [(char_start, char_end, text)]。"""
    if len(chunks) != len(vecs):
        raise ValueError("切片数与向量数不一致")
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    dim = int(vecs.shape[1]) if len(vecs) else 0
    for i, ((s, e, t), v) in enumerate(zip(chunks, vecs)):
        conn.execute(
            "INSERT INTO chunks(doc_id, chunk_index, text, char_start, char_end,"
            " embedding, embed_dim, embed_model) VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, i, t, s, e, v.astype(np.float16).tobytes(), dim, model))
    return len(chunks)


def delete(conn, doc_id: int) -> None:
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))


def has_vectors(conn, model: str) -> bool:
    return conn.execute("SELECT 1 FROM chunks WHERE embed_model=? LIMIT 1", (model,)).fetchone() is not None


def search(conn, qvec: np.ndarray, model: str, top_k: int = 50) -> list[dict]:
    """余弦 Top-K，同一文档只保留最佳切片。qvec 需已 L2 归一化。"""
    rows = conn.execute(
        "SELECT id, doc_id, text, embedding, embed_dim FROM chunks WHERE embed_model=?",
        (model,)).fetchall()
    if not rows:
        return []
    dim = rows[0]["embed_dim"]
    mat = np.vstack([np.frombuffer(r["embedding"], dtype=np.float16).astype(np.float32)
                     for r in rows if r["embed_dim"] == dim])
    rows = [r for r in rows if r["embed_dim"] == dim]
    q = np.asarray(qvec, dtype=np.float32).reshape(-1)
    if q.shape[0] != dim:
        return []
    sims = mat @ q
    order = np.argsort(-sims)
    best: dict[int, dict] = {}
    for idx in order:
        r = rows[int(idx)]
        if r["doc_id"] not in best:
            best[r["doc_id"]] = {"doc_id": r["doc_id"], "chunk_id": r["id"],
                                 "score": float(sims[idx]), "text": r["text"]}
            if len(best) >= top_k:
                break
    return list(best.values())
