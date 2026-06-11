"""
元数据路由 — 受控词表下发

端点：
  GET /meta/permissions  — 返回 acl_tags + clearance_levels + sensitivity_levels 的合法值

设计原因：
  acl_tags 和 sensitivity_level 采用受控词表（不允许手填），
  前端从此接口拉取选项后渲染下拉/多选，消除 typo 风险。
  && 操作符要求字符串完全一致，一个 typo 会让权限失效或误授。
"""

from typing import Any

from fastapi import APIRouter, Depends

from app.config import ALLOWED_ACL_TAGS, CLEARANCE_LABELS, SENSITIVITY_ORDINALS
from app.core.security import get_current_user
from app.db.models import User

router = APIRouter(prefix="/meta", tags=["meta"])


@router.get(
    "/permissions",
    summary="获取权限受控词表（acl_tags / clearance_levels / sensitivity_levels）",
)
async def get_permissions(
    _current_user: User = Depends(get_current_user),   # 登录才能拉取词表
) -> dict[str, Any]:
    """
    返回系统允许的权限配置选项。

    前端的所有权限相关表单（文档上传、用户管理）均从此接口获取选项，
    禁止自由文本输入以防止 acl_tags 与 permission_tags 因 typo 无法匹配。

    Returns:
        {
          "acl_tags": ["finance", "hr", ...],        # 文档和用户的合法权限标签
          "sensitivity_levels": ["public", ...],     # 文档敏感等级合法值
          "clearance_levels": [                      # 用户密级选项（label + 序数）
            {"value": 0, "label": "public"},
            {"value": 1, "label": "internal"},
            {"value": 2, "label": "confidential"},
          ]
        }
    """
    return {
        "acl_tags": ALLOWED_ACL_TAGS,
        "sensitivity_levels": list(SENSITIVITY_ORDINALS.keys()),   # ["public","internal","confidential"]
        "clearance_levels": [
            {"value": i, "label": label}
            for i, label in enumerate(CLEARANCE_LABELS)
        ],
    }
