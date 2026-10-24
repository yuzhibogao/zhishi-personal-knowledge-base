"""Allowlisted source distribution, excluding local data and historical reports."""
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[2]
WORK=ROOT/'work'
ROOT_FILES=['README.md','CHANGELOG.md','LICENSE_NOTICE.md','THIRD_PARTY_NOTICES.md','.gitignore']
DOC_FILES=['使用指南.md','模型与配置.md','检索与分类.md','发布与隐私.md','架构与维护.md']
WORK_FILES=['README.md','.gitignore','.env.example','launch.py','portable_entry.py','requirements.txt',
 'requirements-runtime.txt','requirements-server.txt','requirements-optional.txt','requirements-build.txt','requirements.lock.txt',
 'install_windows.bat','start.bat','start_backend.bat','start_floatbox.bat','Dockerfile','docker-compose.yml']

def files():
    result=[ROOT/x for x in ROOT_FILES]+[ROOT/'docs'/x for x in DOC_FILES]+[WORK/x for x in WORK_FILES]
    result += [WORK/'scripts'/x for x in ('build_portable.py','package_source.py')]
    for folder in ('app','floatbox'):
        result += [p for p in (WORK/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py','.html','.css','.js','.txt','.png','.ico','.svg')]
    return sorted(set(result))

def main():
    out=WORK/'dist/Zhishi-Source.zip';out.parent.mkdir(exist_ok=True)
    with ZipFile(out,'w',ZIP_DEFLATED) as archive:
        for p in files():archive.write(p,'Zhishi-Source/'+p.relative_to(ROOT).as_posix())
    print(out)

if __name__=='__main__':main()
