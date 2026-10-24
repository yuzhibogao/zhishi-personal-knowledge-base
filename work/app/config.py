"""配置加载：读取项目根目录的 .env（无第三方依赖）。

关键决策：
- 已存在的真实环境变量优先于 .env（便于测试与部署覆盖）。
- 默认值开箱即用：LLM / Embedding 默认 mock，无需 key。
"""
import os
import sys
from pathlib import Path

ROOT = Path(os.environ['KB_APP_HOME']).resolve() if os.environ.get('KB_APP_HOME') else (Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent.parent)


def load_env(path: Path | None = None) -> dict:
    """解析 KEY=VALUE 行，忽略空行与 # 注释，去除成对引号。返回实际写入的键值。"""
    p = path or (ROOT / ".env")
    loaded = {}
    if not p.exists():
        return loaded
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v
            loaded[k] = v
    return loaded


load_env()
