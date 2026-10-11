import os

DB_PATH = os.environ.get("SHIGUANG_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "shiguang.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
PASSWORD = os.environ.get("SHIGUANG_PASSWORD", "")
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
GLM_MODEL = os.environ.get("GLM_MODEL", "glm-5.3")
GLM_MODEL_DEEP = os.environ.get("GLM_MODEL_DEEP", "")  # 深度模式用;留空则同 GLM_MODEL
EMBED_MODEL = os.environ.get("EMBED_MODEL", "embedding-3")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))  # HNSW 索引上限 2000 维,embedding-3 指定 1024 维
PORT = int(os.environ.get("PORT", "8300"))
SECRET_PATH = os.path.join(os.path.dirname(DB_PATH), "secret")

SECTIONS = ["长期背景", "当前目标", "长期偏好", "进行中的项目", "经多次观察得到的模式", "近期变化"]
