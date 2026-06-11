"""
检索层权限过滤 — v2: 双维度（acl_tags × clearance_level）

═══════════════════════════════════════════════════════════════════
访问模型（两个正交维度，缺一不可）
═══════════════════════════════════════════════════════════════════

  维度一（横向/群组）: acl_tags && :user_tags
    OR 语义：文档可见群组集合与用户所属群组有非空交集即可访问。
    "all" 是全员基础标签：每个注册用户默认持有，acl_tags=["all"] 的文档对所有登录用户可见。
    不对 "all" 做特例处理——通过正常 && 运算命中，逻辑统一无例外分支。

  维度二（纵向/密级）: sensitivity_ordinal <= :user_clearance
    有序比较：文档密级序数 ≤ 用户密级序数才可访问。
    public(0) < internal(1) < confidential(2)
    用户 clearance_level=0（public）只能看 sensitivity_ordinal=0 的公开文档。

  组合：两个维度 AND（必须同时满足）：
    COMBINED_FILTER_SQL = "(acl_tags && :user_tags AND sensitivity_ordinal <= :user_clearance)"

═══════════════════════════════════════════════════════════════════
去掉的逻辑（设计说明）
═══════════════════════════════════════════════════════════════════

  【去掉了所有者例外 OR uploaded_by = :uploader_id】
  本系统文档由管理员代各部门上传。若保留所有者例外，
  上传文档的管理员将绑过权限读到所有历史上传，破坏密级隔离。
  权限覆盖（谁能看什么）完全由 acl_tags + clearance_level 两维度决定。

═══════════════════════════════════════════════════════════════════
安全不变量（每次修改必须维护）
═══════════════════════════════════════════════════════════════════

  【正确做法】COMBINED_FILTER_SQL 同时注入 vector_results 和 keyword_results
  两个 CTE，RRF 融合前各自独立过滤，融合后不再补过滤。

  【错误做法 1 — 后置过滤】先全量检索再过滤：中间结果含越权数据，造成泄露。

  【错误做法 2 — 遗漏一条路】向量路加了过滤，关键词路忘了加——
  用户通过关键词路召回越权文档，融合后泄露。
  本模块封装为共用片段，两条路分别注入，消除漏加风险。

═══════════════════════════════════════════════════════════════════
"无内容"安全话术（存在性保护）
═══════════════════════════════════════════════════════════════════

  检索结果为空时，无论是"确实没有"还是"有但无权限"，
  上层（llm_client.py）返回同一句话，不泄露文档存在性。
  此处过滤层不负责话术——只负责过滤正确性。

性能：
  acl_tags GIN 索引 (idx_chunks_acl_tags) 命中 && 操作符；
  sensitivity_ordinal B-tree 索引 (idx_chunks_sensitivity_ordinal) 命中 <= 操作符。
"""

from sqlalchemy import Integer, Text, bindparam
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql.elements import BindParameter


# ──────────────────────────────────────────────────────────────
# SQL 过滤片段常量
# ──────────────────────────────────────────────────────────────

# 双维度组合过滤条件（在 vector_results 和 keyword_results 两个 CTE 的 WHERE 中使用）
COMBINED_FILTER_SQL = (
    "acl_tags && :user_tags AND sensitivity_ordinal <= :user_clearance"
)

# 永假条件（user_tags 为空时使用：默认拒绝，不进 DB 查询）
ACL_DENY_ALL_SQL = "false"


def build_acl_bindparam() -> BindParameter[list[str]]:  # type: ignore[type-arg]
    """
    构建 :user_tags 绑定参数（PostgreSQL text[] 类型）。

    必须在 text().bindparams() 中使用，使 SQLAlchemy / asyncpg
    能正确地将 Python list[str] 编码为 PostgreSQL text[] 数组。

    user_tags 来自 current_user.permission_tags（服务端从 DB 加载），
    不接受请求体中任何权限声明。
    """
    return bindparam("user_tags", type_=ARRAY(Text))


def build_clearance_bindparam() -> BindParameter[int]:
    """
    构建 :user_clearance 绑定参数（PostgreSQL integer 类型）。

    user_clearance 是用户的密级序数（0=public, 1=internal, 2=confidential），
    来自 current_user.clearance_level，由 get_current_user 从 DB 加载，
    绝不接受客户端传入。
    """
    return bindparam("user_clearance", type_=Integer)


def should_deny(user_tags: list[str]) -> bool:
    """
    判断是否应直接拒绝（不进入 DB 查询）。

    条件：user_tags 为空列表时直接返回空结果（默认拒绝原则）。
    正常注册的用户至少有 ["all"] 标签，此函数主要防御系统边界异常。
    """
    return len(user_tags) == 0
