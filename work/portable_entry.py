"""Windows packaged launcher, backend and quick capture entry points."""
import os,sys
from pathlib import Path

def main():
    root=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
    os.environ['KB_APP_HOME']=str(root)
    os.chdir(root)
    # No credentials or DB are bundled. An optional blank template becomes user configuration.
    if not (root/'.env').exists() and (root/'.env.example').exists():
        import shutil
        shutil.copyfile(root/'.env.example',root/'.env')
    if '--backend' in sys.argv:
        import uvicorn
        from app.main import app
        uvicorn.run(app,host='127.0.0.1',port=8000,log_config=None,access_log=False)
    elif '--capture' in sys.argv:
        from floatbox.app import main as capture
        capture()
    else:
        import launch
        launch.main()

if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
