"""Conservative formatting normalization; never fuzzy-merge changed facts or code."""
import re
import unicodedata
import hashlib
import io
import json


def normalized_text(text):
    text = unicodedata.normalize("NFC", text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\u00a0", " ")
    # Whitespace can be meaningful in code, formulas and string literals.
    if "```" in text or re.search(r"(?m)^\s*(?:#include|def |class |int |void )", text):
        return text.strip()
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line).strip()
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        if not lines and re.fullmatch(r"(?:💡\s*)?(?:解答|回答|答案)[：:]?", line):
            continue
        if line:
            lines.append(line)
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    # Chinese line wrapping does not introduce a word boundary.
    text = re.sub(r"(?<=[\u4e00-\u9fff，。；：！？、（）]) +| +(?=[\u4e00-\u9fff，。；：！？、（）])", "", text)
    return text


def workbook_fingerprint(raw):
    import openpyxl
    book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
    try:
        digest=hashlib.sha256()
        def emit(value):
            digest.update(json.dumps(value,ensure_ascii=False).encode())
            digest.update(b"\n")
        for sheet in book.worksheets:
            emit(["sheet",sheet.title])
            pending_empty=0
            for row in sheet.iter_rows():
                values=[(cell.data_type,str(cell.value)) if cell.value is not None else None for cell in row]
                while values and values[-1] is None: values.pop()
                if not values:
                    pending_empty+=1
                    continue
                if pending_empty: emit(["empty_rows",pending_empty])
                pending_empty=0
                emit(values)
        return digest.hexdigest()
    finally:
        book.close()


def duplicate_workbook(conn, raw):
    fingerprint = workbook_fingerprint(raw)
    for row in conn.execute("SELECT doc_id,data FROM originals WHERE lower(filename) LIKE '%.xlsx' ORDER BY doc_id"):
        try:
            if workbook_fingerprint(row["data"]) == fingerprint:
                return row["doc_id"]
        except Exception:
            continue
    return None


def comparison_text(text):
    text=unicodedata.normalize("NFKC",text or "")
    text=re.sub(r"(?m)^\s*```[^\n]*$", "",text)
    text=re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "",text)
    text=re.sub(r"\*\*(.+?)\*\*",r"\1",text)
    text=re.sub(r"`([^`\n]+)`",r"\1",text)
    return re.sub(r"\s+","",text).casefold()

def find_similar(conn,text):
    candidate=comparison_text(text)
    if len(candidate)<80: return None
    # This is a warning, not proof of equality; preserve differing facts/code.
    def shingles(s):
        step=max(1,len(s)//12000)
        return {s[i:i+8] for i in range(0,len(s)-7,step)}
    left=shingles(candidate)
    best=None;best_score=0.90
    for row in conn.execute("SELECT doc_id,clean_text FROM document_bodies ORDER BY doc_id"):
        other=comparison_text(row["clean_text"])
        if not other or min(len(candidate),len(other))/max(len(candidate),len(other))<0.85: continue
        right=shingles(other)
        score=2*len(left & right)/max(1,len(left)+len(right))
        if score>best_score: best=row["doc_id"];best_score=score
    return best
