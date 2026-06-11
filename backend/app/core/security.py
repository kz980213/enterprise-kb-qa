"""
JWT 创建/验证 + get_current_user FastAPI 依赖

安全设计要点（不可改动）：
  1. JWT payload 只存 user_id（sub 字段），绝不存 permission_tags。
     原因：
       · 存在 token 里的权限无法撤销（token 有效期内永久有效）
       · 客户端可以解码（但不能伪造签名）JWT，一旦里面有权限信息就暴露了敏感数据
       · 权限变更（如用户被移出 finance 组）需立即生效，不能等 token 过期

  2. get_current_user 每次请求都从数据库读 User，permission_tags 来自 DB 记录。
     这保证了：
       · 权限撤销立即生效（下次请求即反映）
       · 无法通过伪造 token payload 注入额外权限

  3. user_tags 的传递路径必须是：
       JWT(sub=user_id) → DB查User → current_user.permission_tags → hybrid_search(user_tags=...)
     chat.py 中用注释和变量名明确标注这一约束，防止后续维护者绕过。

  4. 密码哈希使用 bcrypt（passlib CryptContext，schemes=["bcrypt"]），不存明文。
     hashed_password 列存储的是 $2b$ 前缀的 bcrypt 哈希字符串，长度固定 60 字符。
     verify_password(plain, hashed) 在恒定时间内比较，防止时序攻击。

  5. is_admin 管理员标志：
     文档写操作（上传/删除）通过 require_admin 依赖守卫。
     普通登录用户只能问答（只读），不能修改知识库。
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import User
from app.db.session import get_session

# OAuth2PasswordBearer：在 Swagger UI 中启用 "Authorize" 按钮
# tokenUrl 指向 /auth/token（OAuth2 标准密码流端点）
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")

# bcrypt 密码哈希上下文
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ──────────────────────────────────────────────────────────────
# 密码工具
# ──────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """返回 bcrypt 哈希字符串，写入 User.hashed_password。"""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """验证明文密码是否匹配哈希值。"""
    return pwd_context.verify(plain, hashed)


# ──────────────────────────────────────────────────────────────
# JWT 工具
# ──────────────────────────────────────────────────────────────

def create_access_token(user_id: uuid.UUID) -> str:
    """
    创建 JWT。

    payload 只含：
      sub: str(user_id)   — 用户唯一标识
      exp: datetime       — 过期时间
      iat: datetime       — 签发时间

    严禁往 payload 写入 permission_tags 或任何权限相关字段。
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "exp": expire,
        "iat": now,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """
    解码 JWT → 从数据库加载 User。

    权限标签（permission_tags）来自数据库，而非 token。
    调用方通过 current_user.permission_tags 获取权限标签，
    绝不能从请求体或 token payload 获取。

    使用方式：
        @router.get("/protected")
        async def endpoint(current_user: User = Depends(get_current_user)):
            user_tags = current_user.permission_tags  # ← 唯一合法来源
    """
    # 1. 验证并解码 JWT
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id_str: str | None = payload.get("sub")
        if user_id_str is None:
            raise _CREDENTIALS_ERROR
        user_id = uuid.UUID(user_id_str)
    except (JWTError, ValueError):
        raise _CREDENTIALS_ERROR

    # 2. 从数据库加载 User（permission_tags 来自 DB，每次请求都查，确保实时生效）
    user = await session.get(User, user_id)
    if user is None:
        raise _CREDENTIALS_ERROR
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用",
        )

    return user

# ──────────────────────────────────────────────────────────────
# FastAPI 依赖：get_current_user
# ──────────────────────────────────────────────────────────────

async def require_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    管理员权限依赖：要求 current_user.is_admin == True。

    用于保护文档写操作（上传/删除），普通用户调用时返回 403。

    使用方式：
        @router.post("/documents")
        async def upload(current_admin: User = Depends(require_admin)):
            ...  # 只有管理员能到达此处
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="此操作需要管理员权限",
        )
    return current_user


_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="无效的认证凭据",
    headers={"WWW-Authenticate": "Bearer"},
)



