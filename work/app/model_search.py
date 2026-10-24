"""Explicit, request-scoped model-assisted retrieval. Never mutates documents."""
import json
from . import hybrid, runtime
from .llm import build_llm


def search(conn, query, limit, filters, broad=False, progress=None):
    report=progress or (lambda *args,**kwargs:None)
    report(5,"准备本地回退结果")
    options={k:filters.get(k) for k in ('source_type','time_from','time_to','category_path','tag')}
    local_counts={}
    def count_local(percent,stage,**counts):local_counts.update(counts)
    local=hybrid.hybrid_search(conn,query,limit, broad=broad,progress=count_local,**options)
    def fallback(message):
        report(95,'模型不可用，回退本地',fallback=True,failed=1,**local_counts)
        return {'results':local,'search_method':'local_fallback','message':message}
    if not runtime.info(conn)['configured']:
        return fallback('模型尚未配置，已返回本地全文结果。请配置本机 .env 后重启。')
    try:
        model=build_llm(runtime.provider_name())
        model.timeout=35
        report(12,'理解自然语言问题（等待模型）')
        plan=model._ask_json('search_plan',query=query)
        if not isinstance(plan,dict) or not isinstance(plan.get('queries'),list):
            raise ValueError('Invalid query plan')
        queries=[x.strip()[:160] for x in plan['queries'] if isinstance(x,str) and x.strip()][:5]
        if not queries: raise ValueError('Empty query plan')
        report(25,'召回候选资料')
        pool={}
        for q in [query,*queries]:
            for item in hybrid.hybrid_search(conn,q,60,broad=True,**options):
                pool.setdefault(item['id'],item)
        # Small libraries also include non-lexical candidates, avoiding a lexical-only ceiling.
        conds=[];args=[]
        for key,op in [('source_type','='),('time_from','>='),('time_to','<=')]:
            if filters.get(key) is not None:
                col='source_type' if key=='source_type' else 'created_at'
                conds.append(f'd.{col}{op}?');args.append(filters[key])
        if filters.get('category_path'):
            cp=filters['category_path'].strip('/')
            conds.append('(d.category_path=? OR substr(d.category_path,1,?)=?)');args.extend([cp,len(cp)+1,cp+'/'])
        if filters.get('tag'):
            conds.append('EXISTS(SELECT 1 FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE dt.doc_id=d.id AND t.name=?)');args.append(filters['tag'])
        where=' WHERE '+' AND '.join(conds) if conds else ''
        rows=conn.execute('SELECT d.*,b.clean_text FROM documents d JOIN document_bodies b ON b.doc_id=d.id'+where+' ORDER BY d.id DESC LIMIT 61',args).fetchall()
        total=conn.execute('SELECT COUNT(*) FROM documents d'+where,args).fetchone()[0]
        for r in rows:
            if len(pool)>=60:break
            pool.setdefault(r['id'],{'id':r['id'],'title':r['title'],'summary':r['summary'] or '',
                'source_type':r['source_type'],'status':r['status'],'category_path':r['category_path'],
                'tags':(r['tags'] or '').split(','),'snippet':r['clean_text'][:600]})
        report(38,'准备候选正文',total=min(len(pool),60),processed=0,matched=0,excluded=0)
        candidates=[];allowed={}
        for item in list(pool.values())[:60]:
            row=conn.execute('SELECT clean_text FROM document_bodies WHERE doc_id=?',(item['id'],)).fetchone()
            if not row:continue
            body=row[0]; excerpt=body[:700]+'\n'+body[len(body)//2:len(body)//2+300]+'\n'+body[-300:]+'\n'+item.get('snippet','')[:400]
            allowed[item['id']]=(item,excerpt)
            candidates.append({'id':item['id'],'title':item['title'],'category':item['category_path'],
                               'summary':item['summary'][:180],'excerpt':excerpt})
        if not candidates:return {'results':[],'search_method':'model','message':'当前筛选范围内没有资料。'}
        report(45,'判断语义相关性（等待模型）',total=len(candidates))
        out=model._ask_json('search_rank',query=query,candidates=json.dumps(candidates,ensure_ascii=False))
        if not isinstance(out,dict) or not isinstance(out.get('results'),list):raise ValueError('Invalid ranking')
        report(85,'校验原文引用与相关性',processed=len(candidates))
        results=[];seen=set();valid_citations=0
        for hit in out['results'][:60]:
            if not isinstance(hit,dict):continue
            ident=hit.get('id');level=hit.get('relevance');quote=hit.get('quote')
            if type(ident) is not int or ident not in allowed or ident in seen:continue
            if level not in ('direct','related'):continue
            item,excerpt=allowed[ident]
            if not isinstance(quote,str) or len(quote.strip())<4 or len(quote)>500 or quote not in excerpt:continue
            valid_citations+=1
            if not broad and level!='direct':continue
            item=dict(item);seen.add(ident)
            item.update(via=['model'],snippet=quote,matched_terms=[],partial=level!='direct',
                match_reason='模型判断：'+('直接相关' if level=='direct' else '补充相关')+' · '+str(hit.get('reason',''))[:160],
                evidence=quote)
            results.append(item)
        if out['results'] and not valid_citations: raise ValueError('Unverifiable evidence')
        results.sort(key=lambda x:x['partial'])
        report(96,'整理检索结果',processed=len(candidates),matched=len(results),excluded=len(candidates)-len(results))
        return {'results':results[:limit],'search_method':'model','queries':queries,
                'message':f'模型已分析 {len(candidates)} 份候选资料（筛选范围共 {total} 份）。依据正文节选判断，可能遗漏；可切换本地全文检索对照。'}
    except Exception:
        # Do not expose provider response bodies, API credentials or private excerpts.
        return fallback('模型请求超时、服务不可用或返回格式无效，已返回本地全文结果。')
