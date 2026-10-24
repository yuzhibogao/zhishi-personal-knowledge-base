"""A shared library with an explicit, persistent, default-offline processing switch."""
import os

def mode(conn):
    row=conn.execute("SELECT value FROM app_settings WHERE key='processing_mode'").fetchone()
    return "online" if row and row[0]=="online" else "offline"

def provider_name():
    configured=os.environ.get("ONLINE_LLM_PROVIDER") or os.environ.get("LLM_PROVIDER","mock")
    return configured if configured!="mock" else "deepseek"

def info(conn):
    name=provider_name()
    defaults={"deepseek":"deepseek-flash","openai":"gpt-4o-mini","ollama":"qwen2.5:7b"}
    configured=name in defaults and (name=="ollama" or bool(os.environ.get("LLM_API_KEY","").strip()))
    return {"mode":mode(conn),"provider":name,"model":os.environ.get("LLM_MODEL","").strip() or defaults.get(name,""),"configured":configured}

def set_mode(conn, value):
    if value not in {"offline","online"}: raise ValueError("请选择离线或在线模式")
    if value=="online" and not info(conn)["configured"]:
        raise ValueError("尚未配置在线模型，请在 work/.env 填写服务商、模型和密钥后重启后端")
    conn.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('processing_mode',?)",(value,))
    return info(conn)

def capture(conn,doc_id):
    conn.execute("INSERT OR REPLACE INTO document_processing(doc_id,mode) VALUES(?,?)",(doc_id,mode(conn)))

def provider(conn,doc_id):
    from .pipeline import MockLLM
    row=conn.execute("SELECT mode FROM document_processing WHERE doc_id=?",(doc_id,)).fetchone()
    if mode(conn)=="offline" or not row or row[0]!="online": return MockLLM()
    from .llm import build_llm
    backend=build_llm(provider_name())
    class Guard:
        def check(self):
            if mode(conn)!="online": raise RuntimeError("已切换离线，停止后续在线处理。资料已保留，可在离线模式重新分析。")
        def prepare_document(self,text,candidates):
            self.check();return backend.prepare_document(text,candidates)
        def summarize(self,text):
            self.check();return backend.summarize(text)
        def tag_with_aliases(self,text):
            self.check();return backend.tag_with_aliases(text)
        def classify(self,text,summary,candidates):
            self.check();return backend.classify(text,summary,candidates)
    return Guard()
