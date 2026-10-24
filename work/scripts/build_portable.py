"""Build a Windows x64 folder bundle from clean source. No private data inputs."""
from pathlib import Path
import importlib.metadata as md
import os,shutil,subprocess,sys

ROOT=Path(__file__).resolve().parents[1]

def main():
    args=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--windowed',
          '--name','Zhishi','--distpath',str(ROOT/'dist'),'--workpath',str(ROOT/'build'),
          '--specpath',str(ROOT/'build'),'--paths',str(ROOT),
          '--icon',str(ROOT/'app/static/zhishi-icon-v1.ico'),
          '--add-data',str(ROOT/'app/static')+';app/static',
          '--add-data',str(ROOT/'app/prompts')+';app/prompts','--collect-data','jieba',
          '--hidden-import','uvicorn.logging','--hidden-import','uvicorn.loops.auto',
          '--hidden-import','uvicorn.protocols.http.h11_impl','--hidden-import','uvicorn.protocols.websockets.auto',
          '--hidden-import','uvicorn.lifespan.on','--hidden-import','app.main','--hidden-import','floatbox.app']
    for name in ('fastembed','torch','tensorflow','scipy','matplotlib','pandas','IPython','pytest','ranx','markitdown','langchain','onnxruntime'):
        args.extend(['--exclude-module',name])
    for name in ('fastapi','starlette','uvicorn','pydantic','jieba','numpy','python-multipart','pdfplumber','python-docx','openpyxl'):
        args.extend(['--copy-metadata',name])
    args.append(str(ROOT/'portable_entry.py'))
    if '--collect-only' not in sys.argv:
        subprocess.run(args,cwd=ROOT,check=True)
    bundle=ROOT/'dist/Zhishi'
    shutil.copy2(ROOT/'.env.example',bundle/'.env.example')
    for name in ('README.md','LICENSE_NOTICE.md','THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT.parent/name,bundle/name)
    shutil.copytree(ROOT.parent/'docs',bundle/'docs',dirs_exist_ok=True)
    (bundle/'使用说明.txt').write_text('解压整个文件夹后双击Zhishi.exe，无需安装Python。首次为空白文库，本地规则无需API。自己的数据保存在data，模型配置保存在.env。不要只复制exe；不要将使用后的data和.env转发给别人。详情见docs。\n',encoding='utf-8')
    # Preserve license texts from the installed runtime dependency closure.
    for line in (ROOT/'requirements.lock.txt').read_text(encoding='utf-8').splitlines():
        if '==' not in line or line.startswith('#'):continue
        name=line.split('==',1)[0];dist=md.distribution(name)
        dest=bundle/'third_party_licenses'/name;dest.mkdir(parents=True,exist_ok=True)
        (dest/'METADATA.txt').write_text(dist.read_text('METADATA') or '',encoding='utf-8')
        for item in dist.files or []:
            parts=[x.lower() for x in item.parts]
            if not any('license' in x or 'copying' in x or 'notice' in x for x in parts):continue
            src=Path(dist.locate_file(item))
            if src.is_file() and src.stat().st_size<2_000_000:
                relative=Path(*[x for x in item.parts if x not in ('..','.')])
                target=dest/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,target)
    print('Portable bundle:',bundle)

if __name__=='__main__':main()
