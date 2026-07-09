"""
pytest 全局 fixture

关键点：services/trip_store.py 的 DB_PATH 是模块级常量，且模块被 import 的那一刻
就会执行文件底部的 init_db()，往 DB_PATH 指向的 SQLite 文件建表。如果什么都不做，
第一次 `from services import trip_store`（不管发生在哪个测试文件、哪一行）都会
真实地创建/打开项目根目录下的 travel_data.db，污染开发数据库。

两层防护：
1. 会话级兜底：本文件在 pytest 收集阶段最先被导入，此时立刻把环境变量
   TRIP_STORE_DB_PATH 指向一个进程独有的临时文件，再 import 任何项目模块。
   trip_store.py 读取该环境变量来决定 DB_PATH，因此"首次 import 触发的
   init_db()"也只会落在临时文件上。
2. 每测试级隔离：autouse fixture 用 monkeypatch 把 trip_store.DB_PATH 重定向到
   pytest 的 tmp_path，并重新 init_db()，保证每个测试函数拿到一个全新、互不
   干扰的数据库，且测试结束后由 pytest 自动清理临时目录。

两层加在一起，测试全程都不会碰到真实的 travel_data.db（不会创建、不会写入、
mtime 不会变化）。
"""

import os
import sys
import tempfile
from pathlib import Path

# ---- 必须在 import 任何项目模块（尤其是 services.trip_store）之前执行 ----
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_SESSION_DB_DIR = tempfile.mkdtemp(prefix="trip-pytest-session-")
os.environ.setdefault("TRIP_STORE_DB_PATH", str(Path(_SESSION_DB_DIR) / "session.db"))

# 测试全程不触网：即便某处遗漏 monkeypatch，没有 WENDAO_API_KEY 也会让
# CtripClient() 构造直接抛 ValueError，而不是真的发起 HTTP 请求。
os.environ["WENDAO_API_KEY"] = ""
os.environ["AMAP_WEB_SERVICE_KEY"] = ""

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """每个测试函数独立的 SQLite 数据库：重定向 DB_PATH 并重新建表。"""
    from services import trip_store

    test_db_path = tmp_path / "travel_data_test.db"
    monkeypatch.setattr(trip_store, "DB_PATH", test_db_path)
    trip_store.init_db()
    yield trip_store
