"""gunicorn / flask 启动入口。

本地无容器时也可直接运行：
    python3 -I wsgi.py            # 开发服务器
容器内由 gunicorn 使用：
    gunicorn -b 0.0.0.0:8000 -w 2 --threads 4 wsgi:app
"""

import os
import sys

# 本地裸机测试时 Flask 装在工作区 .pylibs；容器内无此目录则跳过。
# 注意 -I（隔离模式）下连脚本目录都不会进 sys.path，需要显式注入。
_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.join(_HERE, ".pylibs")):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

from rescue.app import create_app  # noqa: E402

app = create_app(os.environ.get("RESCUE_DATA_DIR", "/data"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
