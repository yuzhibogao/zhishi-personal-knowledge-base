"""One desktop entry: start/reuse this project's backend, tray and browser."""
import ctypes
import hashlib
import json
import os
import sys
from pathlib import Path
import subprocess
import time
import urllib.request
import webbrowser

FROZEN=getattr(sys,'frozen',False)
ROOT=Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
URL='http://127.0.0.1:8000'
PROJECT=hashlib.sha256(os.path.normcase(os.path.realpath(ROOT/'app/main.py')).encode()).hexdigest()[:16]
HIDDEN=getattr(subprocess,'CREATE_NO_WINDOW',0)

def health():
    try:
        with urllib.request.urlopen(URL+'/api/v1/health',timeout=1) as r:
            return json.load(r).get('data',{})
    except Exception:return None

def main():
    os.chdir(ROOT)
    from tkinter import Tk,messagebox
    window=Tk();window.withdraw()
    kernel=ctypes.windll.kernel32
    kernel.CreateMutexW.restype=ctypes.c_void_p
    mutex=kernel.CreateMutexW(None,False,'Local\\ShizhiLauncher-'+PROJECT)
    if kernel.GetLastError()==183:
        window.destroy();return
    try:
        python=Path(sys.executable) if FROZEN else ROOT/'venv/Scripts/pythonw.exe'
        if not python.exists():raise RuntimeError('尚未安装运行环境，请先运行 work/install_windows.bat。')
        existing=health()
        if existing and existing.get('project_id')!=PROJECT:
            raise RuntimeError('8000 端口正在运行其他服务或旧版后端，请关闭旧后端后再启动本项目。')
        stamp=(ROOT/'.env').stat().st_mtime_ns if (ROOT/'.env').exists() else 0
        if existing and str(existing.get('config_stamp',''))!=str(stamp):
            if messagebox.askyesno('应用新的配置','检测到 .env 已修改。是否重启后端使配置生效？请先完成当前上传或编辑。'):
                subprocess.run(['taskkill','/PID',str(int(existing['pid'])),'/T','/F'],creationflags=HIDDEN,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
                existing=None
        if not existing:
            (ROOT/'data').mkdir(exist_ok=True)
            with (ROOT/'data/launcher-backend.log').open('ab') as log:
                server=subprocess.Popen(([str(python),'--backend'] if FROZEN else [str(python),'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000']),cwd=ROOT,creationflags=HIDDEN,stdout=log,stderr=log)
            deadline=time.monotonic()+40
            while time.monotonic()<deadline:
                current=health()
                if current and current.get('project_id')==PROJECT:break
                if server.poll() is not None:raise RuntimeError('后端未能启动，详情见 work/data/launcher-backend.log。')
                time.sleep(.3)
            else:raise RuntimeError('后端启动时间较长，请稍后再次点击图标，详情见 work/data/launcher-backend.log。')
        # Also recognize the old tray instance, preserving any unsubmitted draft.
        env=dict(os.environ,KB_LAUNCH_ROOT=str(ROOT))
        script="[bool](Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*-m floatbox.app*' -and $_.CommandLine -like ('*'+$env:KB_LAUNCH_ROOT+'*') } | Select-Object -First 1)"
        if FROZEN:script="[bool](Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*--capture*' -and $_.ExecutablePath -eq $env:KB_EXE } | Select-Object -First 1)"
        env['KB_EXE']=str(python)
        probe=subprocess.run(['powershell.exe','-NoProfile','-Command',script],env=env,capture_output=True,text=True,creationflags=HIDDEN,timeout=10)
        if probe.returncode!=0:raise RuntimeError('无法确认悬浮窗状态；后端已启动，请从托盘打开已有悬浮窗。')
        if 'True' not in probe.stdout:
            subprocess.Popen(([str(python),'--capture'] if FROZEN else [str(python),'-m','floatbox.app']),cwd=ROOT,creationflags=HIDDEN)
        webbrowser.open(URL)
    except Exception as e:
        messagebox.showerror('拾知启动提示',str(e))
    finally:
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        kernel.CloseHandle(mutex);window.destroy()

if __name__=='__main__':main()
