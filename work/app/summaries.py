"""Small extractive summaries, no model downloads and no invented padding."""
import re
from collections import Counter

def clean(text):
    text=re.sub(r'(?m)^\s*```[^\n]*$', '',text or '')
    text=re.sub(r'(?m)^\s*(?:#{1,6}\s+|>\s*)','',text)
    text=re.sub(r'\*\*(.+?)\*\*',r'\1',text)
    text=re.sub(r'`([^`\n]+)`',r'\1',text)
    return re.sub(r'[ \t]+',' ',text).strip()

def bounded(text):
    text=re.sub(r'\s+',' ',clean(text)).strip()
    if len(text)<=150:return text
    head=text[:149]
    end=max(head.rfind(x) for x in '。！？；')
    return head[:end+1] if end>=99 else head.rstrip()+'…'

def extract(text):
    text=clean(text)
    if len(re.sub(r'\s+',' ',text))<=150:return bounded(text)
    from .db import tokenize
    # Bound analysis work; sample across a long document, not only its opening.
    if len(text)>24000:text=text[:10000]+'\n'+text[len(text)//2:len(text)//2+7000]+'\n'+text[-7000:]
    parts=[x.strip() for x in re.split(r'(?<=[。！？!?；;])|\n+',text) if len(x.strip())>=12]
    if not parts:return bounded(text)
    stop={'一个','我们','这个','可以','进行','以及','内容','the','and','with','that','this','from','are','for'}
    tokens=[[w.casefold() for w in tokenize(p) if len(w)>1 and w.casefold() not in stop] for p in parts]
    frequency=Counter(w for row in tokens for w in set(row))
    keywords={w for w,_ in frequency.most_common(32)}
    scores=[(len(set(row)&keywords)/(max(len(p),30)**0.5)+(0.8 if re.search('结论|结果|因此|总结|提出|表明|目的',p) else 0),i) for i,(p,row) in enumerate(zip(parts,tokens))]
    chosen=[];size=0
    for _,i in sorted(scores,reverse=True):
        piece=parts[i]
        if piece in [parts[j] for j in chosen]:continue
        chosen.append(i);size+=len(piece)+1
        if size>=125:break
    return bounded(' '.join(parts[i] for i in sorted(chosen)))

def finalize(summary,body):
    value=bounded(summary)
    return value if len(value)>=100 or len(clean(body))<100 else extract(body)
