"""Local DOCX preview: escaped HTML, internal raster images, bounded output."""
import base64
import io
from html import escape
from fastapi import HTTPException
from docx import Document
from docx.text.paragraph import Paragraph
from docx.table import Table
from docx.oxml.ns import qn


def preview(conn, doc_id):
    row=conn.execute("SELECT filename,data FROM originals WHERE doc_id=?",(doc_id,)).fetchone()
    if not row or not row["filename"].lower().endswith(".docx"):
        raise HTTPException(400,"此文档没有 DOCX 原文件")
    document=Document(io.BytesIO(row["data"]))
    image_budget=[8*1024*1024]
    def paragraph(p):
        parts=[]
        for run in p.runs:
            content=escape(run.text).replace("\n","<br>")
            if run.bold: content="<strong>"+content+"</strong>"
            if run.italic: content="<em>"+content+"</em>"
            if run.underline: content="<u>"+content+"</u>"
            parts.append(content)
            for blip in run._r.iter(qn("a:blip")):
                rid=blip.get(qn("r:embed"))
                rel=p.part.rels.get(rid)
                if rel is None or rel.is_external: continue
                part=rel.target_part
                if part.content_type not in {"image/png","image/jpeg","image/gif"}: continue
                raw=part.blob
                if len(raw)>image_budget[0]:
                    parts.append("<p>图片超过预览上限，请下载原件查看。</p>")
                    continue
                image_budget[0]-=len(raw)
                parts.append('<img alt="文档内嵌图片" src="data:'+part.content_type+';base64,'+base64.b64encode(raw).decode()+'">')
        # Some hyperlink text isn't exposed as a run in older python-docx versions.
        if not p.runs and p.text: parts.append(escape(p.text))
        tag="p"
        style=p.style.name if p.style else ""
        if style.startswith("Heading ") and style[-1:] in "123456": tag="h"+style[-1]
        return "<"+tag+">"+("".join(parts) or "<br>")+"</"+tag+">"
    out=[]
    for i, block in enumerate(document.element.body.iterchildren()):
        if i>=1000:
            out.append("<p>预览已截断，请下载原件查看完整文档。</p>")
            break
        if block.tag==qn("w:p"):
            out.append(paragraph(Paragraph(block,document)))
        elif block.tag==qn("w:tbl"):
            table=Table(block,document)
            out.append('<div class="office-table"><table>')
            for row in table.rows[:200]:
                out.append("<tr>"+"".join("<td>"+"".join(paragraph(p) for p in cell.paragraphs)+"</td>" for cell in row.cells[:40])+"</tr>")
            out.append("</table></div>")
    return {"html":"".join(out),"note":"结构化预览保留段落、标题、表格、基础字体和内嵌图片；分页、页眉、文本框与复杂排版请以原文件为准。"}
