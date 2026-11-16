import os
import tempfile

# 必须在导入 app 之前设置，模块级 app = create_app() 会读取
_TMPDIR = tempfile.mkdtemp(prefix="lineage-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
