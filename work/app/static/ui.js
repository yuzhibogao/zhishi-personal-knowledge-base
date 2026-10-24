'use strict';
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let token=sessionStorage.getItem('kb-token')||'', page=1, category='', total=0, readerId=null, blobUrl=null, noticeTimer, requestSerial=0, readerSerial=0;
const statuses={pending:'等待整理',processing:'整理中',stored:'已收录',failed:'处理失败'};
const sources={file:'文件',paste:'笔记',float_window:'快速收集'};
function notice(message){
const box=$('notice');box.replaceChildren(document.createTextNode(message));
const close=document.createElement('button');close.type='button';close.textContent='×';close.setAttribute('aria-label','关闭提示');close.onclick=()=>{clearTimeout(noticeTimer);box.hidden=true;};box.append(close);box.hidden=false;
clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>box.hidden=true,5000);
}
const expiryTimers=new WeakMap();
function expireNotice(container){
clearTimeout(expiryTimers.get(container));
expiryTimers.set(container,setTimeout(()=>{if(container.dataset.busy==='true'){expireNotice(container);return;}container.remove();updateQueueControls();},5000));
}
async function api(path,method='GET',body){const headers={'X-KB-Token':token};if(body && !(body instanceof FormData))headers['Content-Type']='application/json';const res=await fetch('/api/v1'+path,{method,headers,body:body instanceof FormData?body:body?JSON.stringify(body):undefined});const data=await res.json();if(!res.ok||data.code!==200)throw Error(data.msg||'请求失败');return data.data;}
function action(id,fn){$(id).addEventListener('click',()=>Promise.resolve().then(fn).catch(e=>notice(e.message)));}
function navigate(name){if(!['capture','search','library'].includes(name))name='library';$('processingMode').closest('label').style.display=name==='search'?'none':'';document.querySelectorAll('.page').forEach(x=>x.hidden=x.id!==name);document.querySelectorAll('[data-page]').forEach(x=>x.classList.toggle('active',x.dataset.page===name));$('breadcrumb').textContent={capture:'收集资料',search:'查找知识',library:'我的文库'}[name];if(name==='library')loadList().catch(e=>notice(e.message));}
document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>location.hash=b.dataset.page);
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1)));
action('goCapture',()=>location.hash='capture');
action('settingsBtn',()=>{$('token').value=token;$('settings').showModal();});
action('closeSettings',()=>$('settings').close());
action('saveToken',async()=>{token=$('token').value.trim();sessionStorage.setItem('kb-token',token);await loadTree();await loadList();await health();$('settings').close();notice('连接设置已保存');});
async function health(){const h=await api('/health');const local=h.llm_provider==='mock';$('capability').textContent=local?'本地规则整理 · 不调用模型':'模型辅助：'+h.llm_provider;$('modelInfo').innerHTML=`<p>已收录 <strong>${esc(h.documents)}</strong> 份资料</p><p>分类：${local?'离线主题规则（能力有限）':esc(h.llm_provider)}<br>检索：${h.embedding_mode==='mock'?'本地全文检索与中英文别名':esc(h.embedding_mode)+' 向量与关键词'}</p>`;searchHelp();}
$('pasteText').oninput=()=>{$('charCount').textContent=$('pasteText').value.length+' 字';$('textDuplicate')?.remove();};
$('pasteTitle').oninput=()=>$('textDuplicate')?.remove();
function updateQueueControls(){
$('clearUploadNotices').hidden=!$('uploadQueue').children.length;
$('clearUploadNotices').disabled=!$('uploadQueue').querySelector('[data-busy="false"]');
}
function dismissNotice(container, label='关闭提示'){
expireNotice(container);if(container.querySelector('[data-dismiss]'))return;
const close=document.createElement('button');close.type='button';close.className='quiet';close.dataset.dismiss='true';close.textContent='×';close.setAttribute('aria-label',label);
close.onclick=()=>{if(container.dataset.busy==='true')return;container.remove();updateQueueControls();};container.append(close);
}
action('clearUploadNotices',()=>{$('uploadQueue').querySelectorAll('[data-busy="false"]').forEach(x=>x.remove());updateQueueControls();});
function duplicateActions(container, d, saveAgain){
const open=document.createElement('button');open.textContent='打开已有资料';open.onclick=()=>openDoc(d.doc_id).catch(e=>notice(e.message));container.append(open);
const keep=document.createElement('button');keep.textContent='仍然另存';keep.onclick=async()=>{if(!confirm('仍然保存一份独立记录？原有资料不会被覆盖。'))return;keep.disabled=true;container.dataset.busy='true';clearTimeout(expiryTimers.get(container));const close=container.querySelector('[data-dismiss]');if(close)close.disabled=true;updateQueueControls();
try{await saveAgain();await loadTree();await health();}catch(e){notice(e.message);keep.disabled=false;}
finally{container.dataset.busy='false';if(close)close.disabled=false;if(container.isConnected)dismissNotice(container);updateQueueControls();}};container.append(keep);
dismissNotice(container,'忽略');
}
action('saveText',async()=>{
const text=$('pasteText').value.trim();if(!text)return notice('请先填写正文');
const payload={text,title:$('pasteTitle').value,category_path:$('pasteCategory').value};$('saveText').disabled=true;
try{const d=await api('/documents/text','POST',payload);
if(d.created===false){notice(d.duplicate_kind==='similar'?'发现高度相似文字，请比较后决定是否另存；输入已保留':'已有相同文字，未重复保存；输入内容已保留');
let box=$('textDuplicate');if(!box){box=document.createElement('div');box.id='textDuplicate';$('saveText').parentElement.after(box);}box.replaceChildren();box.append(document.createTextNode('已有资料：'+d.title+' '));
duplicateActions(box,d,async()=>{await api('/documents/text','POST',{...payload,allow_duplicate:true});box.remove();notice('已另存一份独立记录');});return;}
$('textDuplicate')?.remove();$('pasteText').value='';$('pasteTitle').value='';$('charCount').textContent='0 字';notice('文字已保存，后台正在整理');await loadTree();await health();
}finally{$('saveText').disabled=false;}});
async function upload(files){const uploadTitle=$('uploadTitle').value.trim(),uploadCategory=$('uploadCategory').value.trim();for(const file of files){const item=document.createElement('div');item.className='queue-item';item.dataset.busy='true';$('uploadQueue').append(item);updateQueueControls();item.textContent=file.name+' · 正在保存…';
try{if(file.size>5*1024*1024)throw Error('超过 5 MB');
const send=async(force=false)=>{const form=new FormData();form.append('file',file);if(uploadTitle)form.append('title',uploadTitle);if(uploadCategory)form.append('category_path',uploadCategory);return api('/documents/upload'+(force?'?allow_duplicate=true':''),'POST',form);};
const d=await send();if(d.created===false){item.textContent=file.name+' · '+(d.duplicate_kind==='file'?'相同文件已收录':d.duplicate_kind==='similar'?'发现高度相似正文，请比较后决定是否另存':'正文相同，文件格式或排版可能不同')+'，未新增。 ';
duplicateActions(item,d,async()=>{await send(true);item.textContent=file.name+' · 已另存，原文件已保留';});
}else item.textContent=file.name+' · 原文件已保存';
}catch(e){item.textContent=file.name+' · '+e.message;item.classList.add('danger');}finally{item.dataset.busy='false';dismissNotice(item);updateQueueControls();}}
$('files').value='';await loadTree();await health();}
$('files').onchange=()=>upload([...$('files').files]).catch(e=>notice(e.message));
$('dropzone').ondragover=e=>{e.preventDefault();$('dropzone').classList.add('drag');};$('dropzone').ondragleave=()=>$('dropzone').classList.remove('drag');$('dropzone').ondrop=e=>{e.preventDefault();$('dropzone').classList.remove('drag');upload([...e.dataTransfer.files]).catch(e=>notice(e.message));};
function card(d,search=false){const button=document.createElement('button');button.className='doc-card';const shortText=String(d.summary||'正在整理摘要，请打开阅读正文').replace(/\s+/g,' ').trim();let snippet=esc(shortText);if(search&&d.matched_terms?.length){const words=[...new Set(d.matched_terms)].sort((a,b)=>b.length-a.length).map(w=>w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'));if(words.length){const re=new RegExp('('+words.join('|')+')','gi');/* Highlight text nodes only, never escaped HTML. */const parts=shortText.split(re);snippet=parts.map((s,i)=>i%2?'<mark>'+esc(s)+'</mark>':esc(s)).join('');}}
button.innerHTML=`<div class="doc-meta"><span>${esc(sources[d.source_type]||'资料')} · ${esc(d.category_path||'待分类')}</span><span>${esc(statuses[d.status]||d.status)}</span></div><h3>${esc(d.title)}</h3><p>${snippet}</p><div class="tags">${(Array.isArray(d.tags)?d.tags:[]).map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>${search?`<div class="reason">${esc(d.match_reason||'')}${d.partial?' · 部分相关':''}</div>${d.evidence?`<p class="hint">原文依据：${esc(d.evidence)}</p>`:''}`:''}`;button.onclick=()=>openDoc(d.id).catch(e=>notice(e.message));return button;}
function renderCards(container,items,search=false){container.replaceChildren();if(!search){visibleDocs=items.map(d=>d.id);updateSelection();}if(!items.length){container.innerHTML='<div class="empty">'+(search?'没有找到足够相关的资料。<br>试试简短主题词，或切换“扩大范围”。':'这里还没有资料。<br>添加文件或文字，让知识逐渐积累。')+'</div>';return;}for(const d of items){const c=card(d,search);if(search){container.append(c);continue;}const wrap=document.createElement('div');wrap.className='selectable-card';const check=document.createElement('input');check.type='checkbox';check.checked=selectedDocs.has(d.id);check.setAttribute('aria-label','选择 '+d.title);check.onchange=()=>{if(check.checked)selectedDocs.add(d.id);else selectedDocs.delete(d.id);updateSelection();};wrap.append(check,c);container.append(wrap);}if(!search){visibleDocs=items.map(d=>d.id);updateSelection();}}
async function loadList(){const serial=++requestSerial;const q=new URLSearchParams({page,size:12});if(category)q.set('category_path',category);if($('sourceFilter').value)q.set('source_type',$('sourceFilter').value);const d=await api('/documents?'+q);if(serial!==requestSerial)return;total=d.total;renderCards($('libraryResults'),d.items);$('listTitle').textContent=(category||'全部资料')+' · '+total;$('pageInfo').textContent=`${page} / ${Math.max(1,Math.ceil(total/12))} 页`;$('prevPage').disabled=page<=1;$('nextPage').disabled=page*12>=total;}
action('prevPage',async()=>{if(page>1){page--;await loadList();}});action('nextPage',async()=>{if(page*12<total){page++;await loadList();}});action('reloadList',async()=>{await loadTree();await loadList();await health();});$('sourceFilter').onchange=()=>{page=1;loadList().catch(e=>notice(e.message));};
async function selectCategory(path){category=path;page=1;await loadList();document.querySelectorAll('.tree-row').forEach(x=>x.classList.toggle('selected',x.dataset.path===path));}
action('allDocs',()=>selectCategory(''));
async function loadTree(){const d=await api('/categories');const roots=d.items||d.tree||d;const old=$('searchCategory').value;$('searchCategory').innerHTML='<option value="">所有分类</option>';$('tree').replaceChildren();function count(n){return (n.doc_count||0)+(n.children||[]).reduce((s,c)=>s+count(c),0);}function node(n){const wrap=document.createElement('div'),row=document.createElement('div');row.className='tree-row';row.dataset.path=n.path;row.classList.toggle('selected',n.path===category);const expand=document.createElement('button');expand.textContent=n.children?.length?'▾':'·';expand.setAttribute('aria-label','展开或收起 '+n.name);const choose=document.createElement('button');choose.className='cat-select';choose.textContent=n.name+' ('+count(n)+')';choose.onclick=()=>selectCategory(n.path).catch(e=>notice(e.message));const edit=document.createElement('button');edit.className='tree-edit';edit.textContent='⋯';edit.setAttribute('aria-label','管理 '+n.name);edit.onclick=()=>editCategory(n).catch(e=>notice(e.message));row.append(expand,choose,edit);if(n.path==='未分类'){const auto=document.createElement('button');auto.textContent='一键分类';auto.className='tree-edit';auto.onclick=()=>$('classifyUnclassified').click();row.append(auto);}else{const remove=document.createElement('button');remove.textContent='删除';remove.className='tree-delete';remove.setAttribute('aria-label','删除分类 '+n.path);remove.onclick=()=>deleteCategory(n).catch(e=>notice(e.message));row.append(remove);}wrap.append(row);const option=document.createElement('option');option.value=n.path;option.textContent=n.path;$('searchCategory').append(option);const children=document.createElement('div');children.className='tree-children';for(const c of n.children||[])children.append(node(c));wrap.append(children);expand.onclick=()=>{children.hidden=!children.hidden;expand.textContent=children.hidden?'▸':'▾';expand.setAttribute('aria-expanded',String(!children.hidden));};return wrap;}for(const n of roots)$('tree').append(node(n));$('searchCategory').value=old;syncCategoryChoices();}
async function deleteCategory(n){
 if(!confirm('确认删除「'+n.path+'」及其全部子分类？文献与原文件不会删除，均会移入“未分类”，稍后可一键分类。'))return;
 const result=await api('/categories/'+n.id,'DELETE');
 if(category===n.path||category.startsWith(n.path+'/')){category='未分类';page=1;}
 await loadTree();await loadList();notice('分类已删除，'+result.moved_documents+' 份资料移入未分类');
}
async function editCategory(n){const choice=prompt('分类：'+n.path+'\n输入新名称可重命名，删除请使用右侧“删除”按钮。',n.name);if(choice===null||choice===n.name)return;if(choice==='/删除')return deleteCategory(n);await api('/categories/'+n.id,'PATCH',{name:choice});if(category===n.path||category.startsWith(n.path+'/'))category='';await loadTree();await loadList();}
action('newCategory',async()=>{const path=prompt('输入分类路径，例如：研究/信息检索');if(!path?.trim())return;await api('/categories','POST',{path:path.trim()});await loadTree();$('moveTarget').value=path.trim();category='';page=1;location.hash='library';await loadList();notice('分类已创建，可勾选现有资料并移入该分类');});
let searchSerial=0, searchBusy=false;
function searchHelp(){const model=$('searchMethod').value==='model';$('searchHint').textContent=model?'模型辅助：将问题和最多60份候选资料的摘要、正文节选发送给已配置的模型服务（可能产生费用）。严格匹配仅保留直接相关，扩大范围也展示补充相关。此选择独立于入库整理模式。':'本地全文：匹配标题、摘要、分类、标签和正文。严格匹配要求命中至少75%的概念，扩大范围命中任一概念即可。';}
$('searchMethod').onchange=()=>{++searchSerial;searchHelp();$('searchStatus').textContent='';$('searchProgressPanel').hidden=true;$('searchResults').replaceChildren();};
function showSearchProgress(job){
 $('searchProgressPanel').hidden=false;$('searchProgress').value=job.percent;$('searchPercent').textContent=job.percent+'%';
 const elapsed=Math.max(0,Math.floor(Date.now()/1000-job.started));$('searchStage').textContent=job.stage+' · '+elapsed+'秒';
 $('searchCounts').textContent=`候选 ${job.total} · 已检查 ${job.processed}/${job.total} · 匹配 ${job.matched} · 未纳入 ${job.excluded} · 异常 ${job.failed}`+(job.returned!==undefined?` · 展示 ${job.returned}`:'')+(job.fallback?' · 已回退本地':'');
 $('searchTrace').replaceChildren();for(const item of job.trace||[]){const li=document.createElement('li');li.textContent=`${item.elapsed}秒 · ${item.percent}% · ${item.stage}`;$('searchTrace').append(li);}
}
$('searchForm').onsubmit=async e=>{
 e.preventDefault();if(searchBusy)return;const serial=++searchSerial;searchBusy=true;
 const controls=[$('searchForm').querySelector('button'),$('searchMethod'),$('searchMode'),$('searchCategory'),$('query')];controls.forEach(x=>x.disabled=true);
 $('searchStatus').textContent='正在提交检索任务…';$('searchResults').replaceChildren();$('searchProgressPanel').hidden=true;
 try{
   let job=await api('/search/jobs','POST',{query:$('query').value,limit:50,method:$('searchMethod').value,mode:$('searchMode').value,filters:{category_path:$('searchCategory').value||null}});
   const started=Date.now();let failures=0;
   while(serial===searchSerial){
     showSearchProgress(job);
     if(job.state==='done'){
       const d=job.result;renderCards($('searchResults'),d.results,true);$('searchStatus').textContent=d.message||'检索完成。';
       if(d.queries?.length)$('searchStatus').textContent+=' 检索扩展：'+d.queries.join('；');break;
     }
     if(job.state==='failed')throw Error(job.stage);
     if(Date.now()-started>300000)throw Error('等待超过5分钟，请稍后重试；后台任务可能仍在结束处理中。');
     $('searchStatus').textContent='任务进行中，无需重复提交。';await new Promise(resolve=>setTimeout(resolve,1000));
     try{job=await api('/search/jobs/'+job.job_id);failures=0;}catch(err){if(++failures>=3)throw err;$('searchStatus').textContent='连接暂时中断，正在恢复进度…';}
   }
 }catch(err){if(serial===searchSerial){$('searchStatus').textContent=err.message;$('searchStage').textContent='检索中断 · '+err.message;}}
 finally{searchBusy=false;controls.forEach(x=>x.disabled=false);}
};
searchHelp();
function excelColumn(index){let result='';for(let n=index+1;n>0;n=Math.floor((n-1)/26))result=String.fromCharCode(65+(n-1)%26)+result;return result;}
function renderNote(text){
let inCode=false,code=[],out=[];
for(const line of String(text||'').split('\n')){
 if(/^\s*```/.test(line)){if(inCode){out.push('<pre class="code-block"><code>'+esc(code.join('\n'))+'</code></pre>');code=[];}inCode=!inCode;continue;}
 if(inCode){code.push(line);continue;}
 let safe=esc(line).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`([^`]+)`/g,'<code>$1</code>');
 const heading=/^(#{1,6})\s+(.*)$/.exec(line);
 if(heading)out.push('<h'+heading[1].length+'>'+esc(heading[2])+'</h'+heading[1].length+'>');
 else if(line.trim())out.push('<p>'+safe+'</p>');
}
if(code.length)out.push('<pre class="code-block"><code>'+esc(code.join('\n'))+'</code></pre>');
return '<article class="note-document">'+out.join('')+'</article>';
}
function releaseBlob(){if(blobUrl){URL.revokeObjectURL(blobUrl);blobUrl=null;}}
$('reader').addEventListener('close',()=>{readerSerial++;releaseBlob();readerId=null;});action('closeReader',async()=>{if(await settleDraft())$('reader').close();});$('reader').addEventListener('cancel',e=>{e.preventDefault();settleDraft().then(ok=>{if(ok)$('reader').close();}).catch(e=>notice(e.message));});
async function openDoc(id){if(!await settleDraft())return;const serial=++readerSerial;const d=await api('/documents/'+id);if(serial!==readerSerial)return;readerId=id;releaseBlob();$('readerContent').innerHTML=`<h2>${esc(d.title)}</h2><p class="hint">${esc(d.category_path||'待分类')} · ${esc(statuses[d.status]||d.status)}${d.error_message?' · '+esc(d.error_message):''}</p><div class="reader-grid"><div class="reader-body"><div class="tabs"><button id="viewOriginal" ${d.original?'':'disabled'}>原文件</button><button id="viewText">提取文字</button>${d.original?'<button id="downloadOriginal">下载原文件 ↓</button>':''}</div><div id="preview"></div></div><aside class="reader-form"><h3>资料信息</h3><label>标题<input id="editTitle" value="${esc(d.title)}"></label><label>分类路径<input id="editCategory" value="${esc(d.category_path||'')}"></label><label>标签（逗号分隔）<input id="editTags" value="${esc(d.tags||'')}"></label><button id="saveMetadata" class="primary">保存整理</button><button id="reanalyze">重新分析自动信息</button><p class="hint">保留已手工修改的分类和标签。旧文件不会自动恢复原件。</p><button id="deleteDoc" class="danger">删除这份资料</button></aside></div>`;
if(!$('reader').open)$('reader').showModal();installBodyEditor(d);
const showText=()=>{releaseBlob();$('preview').innerHTML=(d.source_type==='file'&&!d.original?'<p class="hint">旧记录仅保留了提取文字。新上传的文件会保留原件。</p>':'')+renderNote(draft?.id===id?draft.original:d.clean_text);};
action('viewText',showText);
async function originalBlob(){const response=await fetch('/api/v1/documents/'+id+'/original',{headers:{'X-KB-Token':token}});if(!response.ok)throw Error('原文件读取失败');return response.blob();}
async function showOriginal(){releaseBlob();if(d.original.mime==='application/pdf'){const b=await originalBlob();if(readerId!==id||serial!==readerSerial)return;blobUrl=URL.createObjectURL(b);$('preview').innerHTML='<iframe title="PDF 原文件阅读器"></iframe>';$('preview').firstElementChild.src=blobUrl;}else if(d.original.filename.toLowerCase().endsWith('.xlsx')){const data=await api('/documents/'+id+'/sheets');if(readerId!==id||serial!==readerSerial)return;$('preview').innerHTML='<p class="hint">数据预览保留工作表与单元格；完整样式、图片和图表请下载原文件阅读。每张表最多显示 200 行、40 列，最多 20 张表。</p><select id="sheetSelect" aria-label="选择工作表"></select><div id="sheetTable" class="sheet"></div>';data.sheets.forEach((s,i)=>{const o=document.createElement('option');o.value=i;o.textContent=s.name;$('sheetSelect').append(o);});function renderSheet(){const s=data.sheets[Number($('sheetSelect').value)];$('sheetTable').innerHTML=s?'<table><thead><tr><th>行</th>'+Array.from({length:Math.max(0,...s.rows.map(r=>r.length))},(_,i)=>'<th>'+excelColumn(i)+'</th>').join('')+'</tr></thead><tbody>'+s.rows.map((r,i)=>'<tr><th>'+(i+1)+'</th>'+r.map(v=>'<td>'+esc(v)+'</td>').join('')+'</tr>').join('')+'</tbody></table>':'';}$('sheetSelect').onchange=renderSheet;renderSheet();}else if(d.original.filename.toLowerCase().endsWith('.docx')){const data=await api('/documents/'+id+'/preview');if(readerId!==id||serial!==readerSerial)return;$('preview').innerHTML='<p class="hint">'+esc(data.note)+'</p><article class="office-document">'+data.html+'</article>';}else{showText();}}
if(d.original){action('viewOriginal',showOriginal);action('downloadOriginal',async()=>{const b=await originalBlob(),u=URL.createObjectURL(b),a=document.createElement('a');a.href=u;a.download=d.original.filename;a.click();setTimeout(()=>URL.revokeObjectURL(u),30000);});try{await showOriginal();}catch(e){showText();notice(e.message);}}else showText();
action('saveMetadata',async()=>{if(!await settleDraft())return;await api('/documents/'+id,'PATCH',{title:$('editTitle').value,category_path:$('editCategory').value||undefined,tags:$('editTags').value.split(/[,，]/).map(x=>x.trim()).filter(Boolean)});notice('已保存');await loadTree();await loadList();await openDoc(id);});
action('reanalyze',async()=>{if(!await settleDraft())return;if(!confirm('重新生成摘要、自动分类、标签与检索索引？手工修正会保留；配置云模型时会发送文档节选。'))return;$('reanalyze').disabled=true;$('reanalyze').textContent='分析中…';try{const r=await api('/documents/'+id+'/reprocess?refresh=true','POST');notice(r.status==='stored'?'分析完成':'处理未完成：'+r.error_message);await loadTree();await loadList();if(readerId===id)await openDoc(id);}finally{if($('reanalyze'))$('reanalyze').disabled=false;}});
action('deleteDoc',async()=>{if(!await settleDraft())return;if(!confirm('确定删除这份资料及其原文件？'))return;await api('/documents/'+id,'DELETE');$('reader').close();await loadTree();await loadList();notice('资料已删除');});}


const selectedDocs=new Set();let visibleDocs=[],draft=null,settling=null;
function updateSelection(){ $('selectionCount').textContent='已选 '+selectedDocs.size+' 份';$('moveSelected').disabled=!selectedDocs.size; }
function syncCategoryChoices(){
 const paths=[...$('searchCategory').options].map(x=>x.value).filter(Boolean),previous=$('moveTarget').value;
 $('categoryOptions').replaceChildren();$('moveTarget').innerHTML='<option value="">选择目标分类</option>';
 for(const path of paths){for(const id of ['categoryOptions','moveTarget']){const o=document.createElement('option');o.value=path;o.textContent=path;$(id).append(o);}}
 $('moveTarget').value=previous;
}
action('selectPage',()=>{visibleDocs.forEach(id=>selectedDocs.add(id));return loadList();});
action('clearSelection',()=>{selectedDocs.clear();return loadList();});
action('moveSelected',async()=>{const target=$('moveTarget').value;if(!target)return notice('请选择目标分类');if(!confirm('将选中的 '+selectedDocs.size+' 份资料移入「'+target+'」？每份资料使用一个主分类。'))return;const d=await api('/library/move','POST',{doc_ids:[...selectedDocs],category_path:target});selectedDocs.clear();await loadTree();await loadList();notice('已归类 '+d.moved+' 份资料');});
action('pickExisting',async()=>{if(!category)return notice('请先点击左侧目标分类');$('moveTarget').value=category;await selectCategory('');notice('勾选要加入的资料，然后点击“移入分类”');});
function isDirty(){return draft&&$('bodyEditor')&&$('bodyEditor').value!==draft.original;}
function installBodyEditor(d){
 draft=null;if(!['paste','float_window'].includes(d.source_type))return;
 draft={id:d.id,hash:d.content_hash,original:d.clean_text||''};
 const button=document.createElement('button');button.textContent='编辑正文';button.id='editBody';$('viewText').after(button);
 const area=document.createElement('div');area.id='bodyEditArea';area.hidden=true;
 area.innerHTML='<p class="hint">退出阅读时确认是否保存。保存后将重新整理摘要与检索索引。</p><textarea id="bodyEditor" aria-label="编辑正文"></textarea><button id="saveBodyNow" class="primary">保存正文</button>';
 $('preview').after(area);$('bodyEditor').value=d.clean_text||'';
 button.onclick=()=>{area.hidden=!area.hidden;$('preview').hidden=!area.hidden;button.textContent=area.hidden?'编辑正文':'返回阅读';};
 $('saveBodyNow').onclick=()=>settleDraft().catch(e=>notice(e.message));
}
function settleDraft(){
 if(settling)return settling;if(!isDirty())return Promise.resolve(true);
 settling=new Promise(resolve=>{
  const dialog=$('saveChanges');$('bodySaveError').textContent='';dialog.showModal();
  const done=value=>{dialog.close();settling=null;resolve(value);};
  $('saveBodyYes').onclick=async()=>{
   for(const id of ['saveBodyYes','saveBodyNo','saveBodyCancel'])$(id).disabled=true;
   try{const text=$('bodyEditor').value;const result=await api('/documents/'+draft.id+'/body','PATCH',{text,expected_hash:draft.hash});draft.hash=result.content_hash;draft.original=text;$('preview').innerHTML=renderNote(text);notice('正文已保存，摘要与索引正在更新');done(true);loadList().catch(e=>notice(e.message));}
   catch(e){$('bodySaveError').textContent=e.message+'；草稿已保留。';}
   finally{for(const id of ['saveBodyYes','saveBodyNo','saveBodyCancel'])$(id).disabled=false;}
  };
  $('saveBodyNo').onclick=()=>{$('bodyEditor').value=draft.original;done(true);};
  $('saveBodyCancel').onclick=()=>done(false);
  dialog.oncancel=e=>{e.preventDefault();if(!$('saveBodyYes').disabled)done(false);};
 });return settling;
}
window.addEventListener('beforeunload',e=>{if(isDirty()){e.preventDefault();e.returnValue='';}});
const originalHealth=health;
health=async()=>{await originalHealth();const s=await api('/settings/processing');$('processingMode').value=s.mode;$('processingMode').dataset.current=s.mode;};
$('processingMode').onchange=async()=>{
 const select=$('processingMode'),previous=select.dataset.current||'offline',value=select.value;
 if(value==='online'&&!confirm('模型辅助会将新录入或主动重新分析的资料节选发送给所配置的模型服务。共用现有文库，是否启用？')){select.value=previous;return;}
 select.disabled=true;try{await api('/settings/processing','PUT',{mode:value});await health();notice(value==='offline'?'已切回本地规则；已发出的模型请求可能仍在完成':'已启用模型辅助；原有资料不会自动重新分析');}catch(e){select.value=previous;notice(e.message);}finally{select.disabled=false;}
};
let classificationTimer;
async function classificationStatus(){
 const s=await api('/library/classification-status');$('classifyUnclassified').disabled=s.running;
 $('classificationProgress').textContent=s.total?`已处理 ${s.done}/${s.total} · 已分类 ${s.classified} · 仍未分类 ${s.unclassified} · 失败 ${s.failed} · 跳过 ${s.skipped}`:'';
 clearTimeout(classificationTimer);
 if(s.running)classificationTimer=setTimeout(()=>classificationStatus().catch(e=>notice(e.message)),2000);
 else if(s.total){await loadTree();if(location.hash==='#library')await loadList();}
}
action('classifyUnclassified',async()=>{
 const mode=$('processingMode').value;
 if(!confirm('按当前'+(mode==='online'?'模型辅助':'本地规则')+'模式整理全部未分类资料？已归入其他分类的资料不受影响。'+(mode==='online'?'相关文档节选会发送给已配置的模型服务。':'')))return;
 $('classifyUnclassified').disabled=true;
 try{const s=await api('/library/classify-unclassified','POST');notice(s.total?'一键分类已开始':'目前没有未分类资料');await classificationStatus();}catch(e){$('classifyUnclassified').disabled=false;throw e;}
});
navigate(location.hash.slice(1));Promise.all([loadTree(),health(),classificationStatus()]).catch(e=>notice(e.message));
