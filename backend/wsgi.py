"""
WSGI 入口：供 gunicorn / waitress 等生产服务器加载

用法（在 backend/ 目录下）：
    gunicorn -w 1 -b 0.0.0.0:5000 wsgi:app
"""
from app import create_app
from config import ProductionConfig

app = create_app(ProductionConfig)
