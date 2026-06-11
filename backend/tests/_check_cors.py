"""验证 CORS_ORIGINS 解析逻辑。"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-32-chars-placeholder!!!")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "test")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "test")

def parse(raw: str) -> list[str]:
    return [o.strip() for o in raw.split(",") if o.strip()]

# ── Case 1: 覆盖 env var（含空格）─────────────────────────────────
os.environ["CORS_ORIGINS"] = "https://my-app.vercel.app, http://localhost:5173 "

from app.config import get_settings
get_settings.cache_clear()
settings = get_settings()

assert isinstance(settings.cors_origins, str), f"期望 str，得到: {type(settings.cors_origins)}"
parsed = parse(settings.cors_origins)
assert parsed == ["https://my-app.vercel.app", "http://localhost:5173"], (
    f"期望两项列表，得到: {parsed}"
)
print("PASS [1]: 逗号分隔字符串原样读入，parse() 正确去除空格")

# ── Case 2: 默认值（不设 CORS_ORIGINS 时）────────────────────────
del os.environ["CORS_ORIGINS"]
get_settings.cache_clear()
settings2 = get_settings()
parsed2 = parse(settings2.cors_origins)
assert "http://localhost:5173" in parsed2, f"默认值中应包含 localhost:5173，得到: {parsed2}"
print(f"PASS [2]: 默认值正确: {parsed2}")
