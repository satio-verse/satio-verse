"""原型的测试和项目自己的测试用同一套隔离: 把 tests/conftest.py 里的 fixture 原样搬过来 (微信、保管箱、工作台、服务看守的目录
都换成临时目录, 去掉真的 DeepSeek key, 不录系统声音). 这样 pytest 起的测试服务端不读、不写真的 ~/.worldmodel.

只能用 pytest 跑 (在项目根目录):
    python -B -m pytest prototypes/link -q -p no:cacheprovider
直接 python 跑测试文件没有这些隔离, 别那么做.
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (HERE, ROOT / "tests", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_spec = importlib.util.spec_from_file_location("wm_project_test_conftest", ROOT / "tests" / "conftest.py")
_project = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_project)
for _name, _value in vars(_project).items():  # fixture 的名字都以下划线开头, import * 带不过来, 逐个搬
    if not _name.startswith("__"):
        globals()[_name] = _value
