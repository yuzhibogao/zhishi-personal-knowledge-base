"""V0.1 FastAPI 后端：文本录入 / TXT·MD 上传 / 关键词检索 / 列表 / 详情 / 删除。

统一响应格式：{"code": 200, "msg": "success", "data": {...}}
数据先行：入库只依赖 SQLite，不依赖任何 LLM / 向量服务。
"""
import os
import hashlib
from typing import Optional

from charset_normalizer import from_bytes
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config  # noqa: F401  必须最先导入：加载 .env 到环境变量
from . import db, parsers, originals, runtime, locks

DB_PATH = os.environ.get("DB_PATH", str(config.ROOT / "data" / "kb.db"))
# 安全默认：只放行本机来源；悬浮框是原生客户端，不依赖 CORS
CORS_ORIGINS = [o for o in os.environ.get("CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173").split(",") if o]
MAX_UPLOAD = 5 * 1024 * 1024  # 5MB 上传上限
ALLOWED_SOURCE = {"paste", "float_window", "file"}

_CONFIG_STAMP = str(os.stat(config.ROOT / ".env").st_mtime_ns) if (config.ROOT / ".env").exists() else "0"
_PROJECT_ID = hashlib.sha256(os.path.normcase(os.path.realpath(config.ROOT / "app" / "main.py")).encode()).hexdigest()[:16]

app = FastAPI(title="个人知识库", version="0.1.0")
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
@app.middleware("http")
async def _local_guard(request, call_next):
    """本地 API 防护：
    - Host 头必须是本机（防 DNS rebinding）；
    - 若设置了环境变量 KB_API_TOKEN，则除 OPTIONS 与 /health 外必须携带 X-KB-Token。
    默认不设 token（开箱即用），设置后悬浮框通过同名环境变量携带。
    """
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host not in ("127.0.0.1", "localhost", "testserver", "[::1]", ""):
        return JSONResponse({"code": 403, "msg": "非法 Host", "data": {}}, status_code=403)
    token = os.environ.get("KB_API_TOKEN", "")
    if token and request.method != "OPTIONS" and request.url.path.startswith("/api/") and request.url.path != "/api/v1/health":
        import hmac  # 常量时间比较，避免时序侧信道
        if not hmac.compare_digest(request.headers.get("x-kb-token", "").encode("utf-8"),
                                   token.encode("utf-8")):
            return JSONResponse({"code": 401, "msg": "缺少或错误的 X-KB-Token", "data": {}},
                                status_code=401)
    response = await call_next(request)
    # HTML must be refreshed together with its versioned JavaScript after updates.
    if request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])


def ok(data=None, msg: str = "success"):
    return {"code": 200, "msg": msg, "data": data if data is not None else {}}


@app.exception_handler(HTTPException)
async def _http_exc(_, exc: HTTPException):
    return JSONResponse({"code": exc.status_code, "msg": str(exc.detail), "data": {}},
                        status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def _val_exc(_, exc: RequestValidationError):
    return JSONResponse({"code": 422, "msg": "请求参数错误", "data": {"errors": exc.errors()}},
                        status_code=422)


class TextIn(BaseModel):
    text: str
    title: Optional[str] = None
    source_type: Optional[str] = "paste"
    allow_duplicate: bool = False
    category_path: Optional[str] = None


def duplicate_result(conn, doc_id, kind):
    row = conn.execute("SELECT title,status FROM documents WHERE id=?", (doc_id,)).fetchone()
    return ok({"doc_id": doc_id, "duplicate_of": doc_id, "created": False,
               "duplicate_kind": kind, "title": row["title"], "status": row["status"]},
              "已存在，未重复保存")


@app.get("/api/v1/health")
def health():
    """健康检查：除存活外，报告 LLM / Embedding / 向量库配置与库内计数，便于排障。"""
    info = {"status": "up", "version": app.version,"project_id":_PROJECT_ID,"config_stamp":_CONFIG_STAMP,"pid":os.getpid(),
            "llm_provider": os.environ.get("LLM_PROVIDER", "mock"),
            "embedding_mode": os.environ.get("EMBEDDING_MODE", "mock"),
            "vector_store": "float16-blob+numpy", "auto_process": os.environ.get("AUTO_PROCESS", "1") != "0"}
    try:
        with db.session(DB_PATH) as conn:
            info.update(runtime.info(conn))
            info["llm_provider"]=runtime.provider_name() if info["mode"]=="online" else "mock"
            info["documents"] = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            info["chunks"] = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            by = conn.execute("SELECT status, COUNT(*) c FROM documents GROUP BY status").fetchall()
            info["by_status"] = {r[0]: r[1] for r in by}
    except Exception as e:
        info["db_error"] = str(e)
    return ok(info)


def _auto_process(background: BackgroundTasks, doc_id: int) -> None:
    """入库后异步触发 摘要->标签->向量化->分类。
    数据先行：此时正文与 FTS 已提交，处理失败只改 status，不影响入库。
    AUTO_PROCESS=0 可关闭（测试 pending 状态用）。"""
    if os.environ.get("AUTO_PROCESS", "1") == "0":
        return
    from . import pipeline
    background.add_task(pipeline.process_by_id, DB_PATH, doc_id)


@app.post("/api/v1/documents/text")
def add_text(body: TextIn, background: BackgroundTasks):
    if len(body.text.encode("utf-8"))>MAX_UPLOAD: raise HTTPException(413,"正文超过 5MB")
    st = body.source_type if body.source_type in {"paste","float_window"} else "paste"
    try:
        with db.session(DB_PATH) as conn:
            # Serialize check-and-insert, including concurrent browser/desktop submissions.
            conn.execute("BEGIN IMMEDIATE")
            dup = db.find_duplicate(conn, body.text)
            if dup is not None and not body.allow_duplicate:
                return duplicate_result(conn, dup, "text")
            if not body.allow_duplicate and dup is None:
                from .dedup import find_similar
                similar=find_similar(conn,body.text)
                if similar is not None: return duplicate_result(conn,similar,"similar")
            doc_id = db.add_document(conn, body.text, body.title, st)
            if body.category_path and body.category_path.strip():
                from . import manage
                manage.update_document(conn,doc_id,category_path=body.category_path)
            runtime.capture(conn,doc_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _auto_process(background, doc_id)
    return ok({"doc_id": doc_id, "status": "pending", "duplicate_of": dup, "created": True})


def decode_bytes(raw: bytes) -> str:
    """自动识别 UTF-8 / GBK / GB18030 等编码。"""
    best = from_bytes(raw).best()
    if best is None:
        raise ValueError("无法识别文件编码")
    return str(best)


@app.post("/api/v1/documents/upload")
async def upload(background: BackgroundTasks, file: UploadFile = File(...),
                 allow_duplicate: bool = False, title: Optional[str] = Form(None), category_path: Optional[str] = Form(None)):
    name = file.filename or "未命名"
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "文件超过 5MB 上限")
    digest = hashlib.sha256(raw).hexdigest()
    if not allow_duplicate:
        with db.session(DB_PATH) as conn:
            existing = conn.execute("SELECT doc_id FROM originals WHERE sha256=? ORDER BY doc_id LIMIT 1",
                                    (digest,)).fetchone()
            if existing:
                return duplicate_result(conn, existing[0], "file")
    try:
        # V0.2：5 种格式统一走 parsers；解析失败抛 ParseError(ValueError)，不存空内容
        parsed_title, text = parsers.parse_file(name, raw)
        title=(title or "").strip() or parsed_title
        with db.session(DB_PATH) as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Recheck under the write lock; parsing happens outside the transaction.
            existing = conn.execute("SELECT doc_id FROM originals WHERE sha256=? ORDER BY doc_id LIMIT 1",
                                    (digest,)).fetchone()
            if name.lower().endswith(".xlsx"):
                from .dedup import duplicate_workbook
                dup = duplicate_workbook(conn, raw)
            else:
                dup = db.find_duplicate(conn, text)
            if not allow_duplicate:
                if existing:
                    return duplicate_result(conn, existing[0], "file")
                if dup is not None:
                    return duplicate_result(conn, dup, "text")
            if not allow_duplicate and dup is None and not name.lower().endswith(".xlsx"):
                from .dedup import find_similar
                similar=find_similar(conn,text)
                if similar is not None: return duplicate_result(conn,similar,"similar")
            doc_id = db.add_document(conn, text, title, "file")
            originals.save(conn, doc_id, name, raw)
            if category_path and category_path.strip():
                from . import manage
                manage.update_document(conn,doc_id,category_path=category_path)
            runtime.capture(conn,doc_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _auto_process(background, doc_id)
    return ok({"doc_id": doc_id, "status": "pending", "title": title,
               "created": True,
               "duplicate_of": dup, "char_count": len(text),
               "needs_chunking": len(text) > parsers.CHUNK_THRESHOLD})


@app.post("/api/v1/search")
def search(body: dict):
    q = (body.get("query") or "").strip()
    if not q:
        raise HTTPException(400, "query 不能为空")
    try:
        limit = max(1, min(100, int(body.get("limit", 10))))
    except (TypeError, ValueError):
        raise HTTPException(400, "limit 必须为整数")
    if len(q)>2000: raise HTTPException(400, "查询请限制在2000字以内")
    filters = body.get("filters") or {}
    if not isinstance(filters,dict): raise HTTPException(400, "filters 必须是对象")
    method=body.get("method","local")
    if method not in {"local","model"}: raise HTTPException(400,"未知检索方式")
    if method=="model":
        from .model_search import search as model_search
        with db.session(DB_PATH) as conn:
            return ok(model_search(conn,q,limit,filters,body.get("mode")=="broad"))
    from . import hybrid
    with db.session(DB_PATH) as conn:
        # V0.4：关键词 + 语义 -> RRF；未配向量或向量路异常时自动退化为纯关键词
        results = hybrid.hybrid_search(
            conn, q, limit,
            source_type=filters.get("source_type"),
            time_from=filters.get("time_from"),
            time_to=filters.get("time_to"),
            category_path=filters.get("category_path"),
            tag=filters.get("tag"), broad=body.get("mode") == "broad")
        return ok({"results": results})


@app.post("/api/v1/search/jobs")
def start_search_job(body: dict):
    from . import search_jobs
    query=body.get('query')
    if not isinstance(query,str) or not query.strip() or len(query)>2000:
        raise HTTPException(400,'请输入1至2000字的查询')
    method=body.get('method','local'); mode=body.get('mode','balanced')
    filters=body.get('filters') or {}
    if method not in ('local','model') or mode not in ('balanced','broad') or not isinstance(filters,dict):
        raise HTTPException(400,'检索参数无效')
    for key in ('category_path','source_type','tag'):
        if filters.get(key) is not None and not isinstance(filters[key],str):raise HTTPException(400,'筛选参数无效')
    for key in ('time_from','time_to'):
        if filters.get(key) is not None and not isinstance(filters[key],(int,float)):raise HTTPException(400,'时间参数无效')
    try:limit=max(1,min(100,int(body.get('limit',50))))
    except (TypeError,ValueError):raise HTTPException(400,'结果数量无效')
    try:return ok(search_jobs.submit(DB_PATH,query.strip(),limit,filters,method,mode=='broad'))
    except ValueError as e:raise HTTPException(429,str(e))


@app.get("/api/v1/search/jobs/{job_id}")
def get_search_job(job_id: str):
    from . import search_jobs
    job=search_jobs.snapshot(job_id)
    if job is None:raise HTTPException(404,'检索任务已过期或服务已重启，请重新搜索')
    return ok(job)


@app.get("/api/v1/documents")
def list_docs(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
              source_type: Optional[str] = None, category_path: Optional[str] = None,
              tag: Optional[str] = None, time_from: Optional[float] = None,
              time_to: Optional[float] = None, status: Optional[str] = None):
    """只查元数据，不加载正文全文。支持按来源/分类(含子分类)/标签/时间/状态筛选。"""
    conds, args = [], []
    if source_type:
        conds.append("source_type=?"); args.append(source_type)
    if status:
        conds.append("status=?"); args.append(status)
    if category_path:
        cp = category_path.strip("/")
        conds.append("(category_path=? OR substr(category_path,1,?)=?)"); args += [cp, len(cp)+1, cp + "/"]
    if tag:
        conds.append("id IN (SELECT dt.doc_id FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE t.name=?)")
        args.append(tag)
    if time_from is not None:
        conds.append("created_at>=?"); args.append(time_from)
    if time_to is not None:
        conds.append("created_at<=?"); args.append(time_to)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with db.session(DB_PATH) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM documents {where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT id,title,source_type,status,char_count,summary,category_path,tags,created_at, "
            f"(SELECT summary FROM document_summaries s WHERE s.doc_id=documents.id AND s.content_hash=documents.content_hash) display_summary, "
            f"(SELECT clean_text FROM document_bodies b WHERE b.doc_id=documents.id) preview_text "
            f"FROM documents {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size]).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        from .summaries import extract
        text=d.pop("preview_text","")
        d["summary"]=d.pop("display_summary",None) or extract(text)
        d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
        items.append(d)
    return ok({"total": total, "page": page, "size": size, "items": items})


@app.get("/api/v1/documents/{doc_id}")
def get_doc(doc_id: int):
    with db.session(DB_PATH) as conn:
        r = conn.execute(
            "SELECT d.*, b.clean_text FROM documents d JOIN document_bodies b ON b.doc_id=d.id "
            "WHERE d.id=?", (doc_id,)).fetchone()
    if not r:
        raise HTTPException(404, "文档不存在")
    with db.session(DB_PATH) as conn:
        original = conn.execute("SELECT filename,mime,sha256,length(data) size FROM originals WHERE doc_id=?", (doc_id,)).fetchone()
    return ok({**dict(r), "original": dict(original) if original else None})


@app.get("/api/v1/documents/{doc_id}/original")
def original_file(doc_id: int, download: bool = False):
    with db.session(DB_PATH) as conn:
        return originals.response(conn, doc_id, download)


@app.get("/api/v1/documents/{doc_id}/preview")
def office_preview(doc_id: int):
    from .office_preview import preview
    with db.session(DB_PATH) as conn:
        return ok(preview(conn, doc_id))


@app.get("/api/v1/documents/{doc_id}/sheets")
def original_sheets(doc_id: int):
    with db.session(DB_PATH) as conn:
        return ok({"sheets": originals.sheets(conn, doc_id)})


@app.post("/api/v1/documents/{doc_id}/original")
async def attach_original(doc_id: int, file: UploadFile = File(...)):
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "文件超过 5MB 上限")
    try:
        _, text = parsers.parse_file(file.filename or "", raw)
        with db.session(DB_PATH) as conn:
            row = conn.execute("SELECT clean_text FROM document_bodies WHERE doc_id=?", (doc_id,)).fetchone()
            if not row:
                raise HTTPException(404, "文档不存在")
            # Reattachment must not silently attach an unrelated file to an old index.
            import re
            if re.sub(r"\s+", "", row[0]) != re.sub(r"\s+", "", text):
                raise HTTPException(409, "内容与旧文档不一致，请作为新文档上传；旧文档保持不变")
            originals.save(conn, doc_id, file.filename, raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ok({"doc_id": doc_id})


@app.post("/api/v1/documents/{doc_id}/reprocess")
def reprocess_doc(doc_id: int, refresh: bool = False):
    """失败重试：重新处理 failed / pending / processing 的文档，只重做缺失步骤。"""
    from . import pipeline
    with locks.document(doc_id), db.session(DB_PATH) as conn:
        if not conn.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
            raise HTTPException(404, "文档不存在")
        runtime.capture(conn,doc_id)
        conn.commit()
        if refresh:
            pipeline.reset_generated(conn, doc_id)
        status = pipeline.process_document(conn, doc_id)
        err = conn.execute("SELECT error_message FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
    return ok({"doc_id": doc_id, "status": status, "error_message": err})


@app.on_event("startup")
def _recover_on_startup():
    """进程重启恢复：重新投递 pending/processing 遗留文档。"""
    from . import pipeline
    try:
        pipeline.recover(DB_PATH)
    except Exception as e:  # 恢复失败不阻止服务启动
        print(f"[startup] 恢复遗留文档失败: {e}")


@app.delete("/api/v1/documents/{doc_id}")
def delete_doc(doc_id: int):
    """V0.6：级联删除（切片/向量/FTS/标签关联），并回退分类与标签计数。"""
    from . import manage
    with locks.document(doc_id), db.session(DB_PATH) as conn:
        if not manage.delete_document_full(conn, doc_id):
            raise HTTPException(404, "文档不存在")
    return ok({"deleted": doc_id})


class DocPatch(BaseModel):
    title: Optional[str] = None
    category_path: Optional[str] = None
    tags: Optional[list[str]] = None


@app.patch("/api/v1/documents/{doc_id}")
def patch_doc(doc_id: int, body: DocPatch):
    """更新标题/分类/标签；分类或标签变更记入 corrections。"""
    from . import classify, manage
    try:
        with locks.document(doc_id), db.session(DB_PATH) as conn:
            r = manage.update_document(conn, doc_id, body.title, body.category_path, body.tags)
    except (ValueError, classify.CategoryLimitError) as e:
        raise HTTPException(400, str(e))
    if r is None:
        raise HTTPException(404, "文档不存在")
    return ok(r)


@app.get("/", include_in_schema=False)
def index_page():
    """最小管理页：由后端直接提供静态 HTML。"""
    path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if not os.path.exists(path):
        raise HTTPException(404, "管理页不存在")
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/api/v1/tags")
def list_tags():
    with db.session(DB_PATH) as conn:
        rows = conn.execute("SELECT id, name, doc_count FROM tags WHERE doc_count>0 "
                            "ORDER BY doc_count DESC, name").fetchall()
    return ok({"items": [dict(r) for r in rows]})


@app.get("/api/v1/categories")
def get_categories():
    from . import manage
    with db.session(DB_PATH) as conn:
        return ok({"tree": manage.category_tree(conn)})


class CatIn(BaseModel):
    path: str
    description: Optional[str] = ""


@app.post("/api/v1/categories")
def add_category(body: CatIn):
    from . import classify
    try:
        with db.session(DB_PATH) as conn:
            cid = classify.create_category(conn, body.path, body.description or "")
    except (ValueError, classify.CategoryLimitError) as e:
        raise HTTPException(400, str(e))
    return ok({"id": cid, "path": body.path})


class CatPatch(BaseModel):
    name: Optional[str] = None
    merge_into: Optional[int] = None


@app.patch("/api/v1/categories/{cat_id}")
def patch_category(cat_id: int, body: CatPatch):
    from . import classify, manage
    try:
        with db.session(DB_PATH) as conn:
            if body.merge_into is not None:
                r = manage.merge_category(conn, cat_id, body.merge_into)
            elif body.name is not None:
                r = manage.rename_category(conn, cat_id, body.name)
            else:
                raise ValueError("需提供 name 或 merge_into")
    except (ValueError, classify.CategoryLimitError) as e:
        raise HTTPException(400, str(e))
    if r is None:
        raise HTTPException(404, "分类不存在")
    return ok(r)


@app.delete("/api/v1/categories/{cat_id}")
def delete_category_api(cat_id: int, migrate_to: Optional[int] = None):
    """删除分类：有文档时必须指定 migrate_to 迁移目标，否则返回 400 并给出警告。"""
    from . import classify, manage
    try:
        with locks.catalog, db.session(DB_PATH) as conn:
            conn.execute("BEGIN IMMEDIATE")
            r = manage.delete_category(conn, cat_id, migrate_to)
    except (ValueError, classify.CategoryLimitError) as e:
        raise HTTPException(400, str(e))
    if r is None:
        raise HTTPException(404, "分类不存在")
    return ok(r)


class ModeIn(BaseModel):
    mode: str

@app.get("/api/v1/settings/processing")
def processing_settings():
    with db.session(DB_PATH) as conn: return ok(runtime.info(conn))

@app.put("/api/v1/settings/processing")
def change_processing(body: ModeIn):
    try:
        with db.session(DB_PATH) as conn: return ok(runtime.set_mode(conn,body.mode))
    except ValueError as e: raise HTTPException(400,str(e))

class BodyEdit(BaseModel):
    text: str
    expected_hash: str

@app.patch("/api/v1/documents/{doc_id}/body")
def edit_body(doc_id: int, body: BodyEdit, background: BackgroundTasks):
    from . import manage,pipeline
    import time
    text=body.text.strip()
    if not text: raise HTTPException(400,"正文不能为空")
    if len(text.encode("utf-8"))>MAX_UPLOAD: raise HTTPException(413,"正文超过 5MB")
    with locks.document(doc_id), db.session(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT * FROM documents WHERE id=?",(doc_id,)).fetchone()
        if not row: raise HTTPException(404,"文档不存在")
        if row["source_type"] not in {"paste","float_window"}: raise HTTPException(400,"仅文字录入资料支持正文编辑")
        if row["content_hash"]!=body.expected_hash: raise HTTPException(409,"正文已在其他窗口修改，请复制当前草稿后重新打开")
        digest=hashlib.sha256(text.encode()).hexdigest()
        if digest==row["content_hash"]: return ok({"content_hash":digest,"unchanged":True})
        # Exclude this record, so editing doesn't match its own old text.
        duplicate=db.find_duplicate(conn,text,exclude_id=doc_id)
        if duplicate: raise HTTPException(409,f"修改后与资料 #{duplicate} 重复，请保留原文或调整内容")
        pipeline.reset_generated(conn,doc_id,commit=False)
        conn.execute("DELETE FROM chunks WHERE doc_id=?",(doc_id,))
        conn.execute("UPDATE document_bodies SET clean_text=?,raw_text=? WHERE doc_id=?",(text,text,doc_id))
        conn.execute("UPDATE documents SET content_hash=?,char_count=?,error_message=NULL,updated_at=? WHERE id=?",(digest,len(text),time.time(),doc_id))
        conn.execute("DELETE FROM fts_docs WHERE rowid=?",(doc_id,))
        conn.execute("INSERT INTO fts_docs(rowid,title,body) VALUES(?,?,?)",(doc_id,db.seg(row["title"]),db.seg(text)))
        runtime.capture(conn,doc_id)
        if row["category_id"]: manage.rebuild_centroid(conn,row["category_id"],manage._model())
    _auto_process(background,doc_id)
    return ok({"doc_id":doc_id,"content_hash":digest,"status":"pending"})

class MoveIn(BaseModel):
    doc_ids: list[int]
    category_path: str

@app.post("/api/v1/library/move")
def move_documents(body: MoveIn):
    from contextlib import ExitStack
    from . import manage
    ids=sorted(set(body.doc_ids))
    if not ids or len(ids)>200: raise HTTPException(400,"请选择 1–200 份资料")
    try:
        with ExitStack() as stack:
            for i in ids: stack.enter_context(locks.document(i))
            with db.session(DB_PATH) as conn:
                conn.execute("BEGIN IMMEDIATE")
                for i in ids:
                    if not conn.execute("SELECT 1 FROM documents WHERE id=?",(i,)).fetchone():
                        raise HTTPException(404,f"资料 #{i} 已不存在，本次未移动")
                for i in ids: manage.update_document(conn,i,category_path=body.category_path)
        return ok({"moved":len(ids),"category_path":body.category_path})
    except ValueError as e: raise HTTPException(400,str(e))


@app.post('/api/v1/library/classify-unclassified')
def classify_unclassified(background: BackgroundTasks):
    from . import category_jobs
    try: ids,mode=category_jobs.prepare(DB_PATH)
    except ValueError as e:raise HTTPException(409,str(e))
    if ids:background.add_task(category_jobs.run,DB_PATH,ids,mode)
    return ok(category_jobs.state())

@app.get('/api/v1/library/classification-status')
def classification_status():
    from . import category_jobs
    return ok(category_jobs.state())
