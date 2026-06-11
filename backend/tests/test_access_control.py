"""
权限隔离回归测试

覆盖的核心场景（对应任务书中"关键路径 pytest"要求）：
  1. ACL tag 横向隔离：finance 用户看不到 hr-only 文档
  2. clearance 纵向隔离：public 密级看不到 confidential 文档
  3. 密级梯度可见性：internal 密级能看到 public + internal，看不到 confidential
  4. "all" 全员标签：仅有 ["all"] 的用户能看到 acl_tags=["all"] 的文档
  5. 空 user_tags 默认拒绝（单元测试，不需要 DB）
  6. should_deny 边界（单元测试）

测试数据：
  通过 fixture 在测试事务内创建，测试结束后自动 rollback，不污染数据库。

运行要求：
  - PostgreSQL 可访问（DATABASE_URL 已配置）
  - migrate_v2_clearance.sql 已执行（sensitivity_ordinal 列存在）
  - 运行：pytest tests/test_access_control.py -v
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import DUMMY_VECTOR


# ──────────────────────────────────────────────────────────────
# 单元测试（不需要 DB）
# ──────────────────────────────────────────────────────────────

def test_should_deny_empty_tags() -> None:
    """user_tags 为空时必须拒绝（默认拒绝原则）。"""
    from app.retrieval.filters import should_deny

    assert should_deny([]) is True


def test_should_deny_nonempty_tags() -> None:
    """user_tags 非空时不拒绝（进入 DB 查询）。"""
    from app.retrieval.filters import should_deny

    assert should_deny(["all"]) is False
    assert should_deny(["finance"]) is False
    assert should_deny(["finance", "hr"]) is False


def test_sensitivity_ordinals_mapping() -> None:
    """sensitivity_level 到序数的映射必须满足偏序：public < internal < confidential。"""
    from app.config import SENSITIVITY_ORDINALS

    assert SENSITIVITY_ORDINALS["public"] < SENSITIVITY_ORDINALS["internal"]
    assert SENSITIVITY_ORDINALS["internal"] < SENSITIVITY_ORDINALS["confidential"]
    assert SENSITIVITY_ORDINALS["public"] == 0
    assert SENSITIVITY_ORDINALS["internal"] == 1
    assert SENSITIVITY_ORDINALS["confidential"] == 2


def test_allowed_acl_tags_contains_all() -> None:
    """受控词表必须包含 'all' 全员标签（注册默认值的合法性前提）。"""
    from app.config import ALLOWED_ACL_TAGS

    assert "all" in ALLOWED_ACL_TAGS


# ──────────────────────────────────────────────────────────────
# Integration test fixtures（需要 DB）
# ──────────────────────────────────────────────────────────────

@pytest.fixture
async def access_control_data(
    db_session: AsyncSession,
) -> AsyncGenerator[dict[str, uuid.UUID], None]:
    """
    在测试事务内创建权限隔离测试所需的 document + chunk 记录。

    chunk 矩阵（acl_tags × sensitivity_ordinal）：
      finance × public(0)       → finance_public
      finance × internal(1)     → finance_internal
      finance × confidential(2) → finance_confidential
      hr      × internal(1)     → hr_internal
      all     × public(0)       → all_public
      all     × internal(1)     → all_internal
    """
    from app.db.models import Document, DocumentChunk, User

    # 创建测试用上传者（管理员，不影响权限过滤，仅满足 FK）
    uploader = User(
        username=f"_test_uploader_{uuid.uuid4().hex[:8]}",
        hashed_password="dummy_bcrypt_hash_not_real",
        permission_tags=["all", "finance", "hr"],
        clearance_level=2,
        is_admin=True,
    )
    db_session.add(uploader)
    await db_session.flush()

    # 测试用 chunk 定义
    cases: list[tuple[list[str], int, str]] = [
        (["finance"], 0, "finance_public"),
        (["finance"], 1, "finance_internal"),
        (["finance"], 2, "finance_confidential"),
        (["hr"],      1, "hr_internal"),
        (["all"],     0, "all_public"),
        (["all"],     1, "all_internal"),
    ]

    chunk_ids: dict[str, uuid.UUID] = {}

    for acl_tags, sens_ordinal, key in cases:
        sens_level = {0: "public", 1: "internal", 2: "confidential"}[sens_ordinal]
        doc = Document(
            filename=f"_test_{key}.pdf",
            source=f"_test_{key}.pdf",
            file_type="pdf",
            acl_tags=acl_tags,
            sensitivity_level=sens_level,
            total_chunks=1,
            uploaded_by=uploader.id,
            content_hash=f"_test_hash_{uuid.uuid4().hex}",   # 唯一，不冲突
        )
        db_session.add(doc)
        await db_session.flush()

        chunk = DocumentChunk(
            document_id=doc.id,
            content=f"权限隔离测试内容 {key}",
            dense_embedding=DUMMY_VECTOR,
            chunk_index=0,
            source=doc.filename,
            acl_tags=acl_tags,
            sensitivity_ordinal=sens_ordinal,
        )
        db_session.add(chunk)
        await db_session.flush()

        chunk_ids[key] = chunk.id

    yield chunk_ids
    # db_session fixture 会自动 rollback，无需手动清理


# ──────────────────────────────────────────────────────────────
# Integration tests — ACL 横向隔离
# ──────────────────────────────────────────────────────────────

async def test_acl_finance_cannot_see_hr(
    db_session: AsyncSession,
    access_control_data: dict[str, uuid.UUID],
) -> None:
    """
    场景 1：finance 用户（clearance=internal=1）看不到 hr-only 文档。
    即使 clearance 足够，acl_tags 不匹配就拒绝。
    """
    from app.retrieval.hybrid_search import hybrid_search

    results = await hybrid_search(
        query_vector=DUMMY_VECTOR,
        query_text="x",          # < 3 字符 → 纯向量路，避免 trigram 干扰
        user_tags=["finance"],
        user_clearance=1,        # internal，密级不是限制因素
        session=db_session,
        top_k=100,
        rrf_k=60,
    )
    returned_ids = {c.chunk_id for c in results}

    assert access_control_data["finance_internal"] in returned_ids, \
        "finance 用户应能看到 finance+internal 的文档"
    assert access_control_data["hr_internal"] not in returned_ids, \
        "finance 用户不应看到 hr-only 的文档"


async def test_acl_hr_cannot_see_finance(
    db_session: AsyncSession,
    access_control_data: dict[str, uuid.UUID],
) -> None:
    """场景 1b：hr 用户看不到 finance-only 文档（对称验证）。"""
    from app.retrieval.hybrid_search import hybrid_search

    results = await hybrid_search(
        query_vector=DUMMY_VECTOR,
        query_text="x",
        user_tags=["hr"],
        user_clearance=1,
        session=db_session,
        top_k=100,
        rrf_k=60,
    )
    returned_ids = {c.chunk_id for c in results}

    assert access_control_data["hr_internal"] in returned_ids
    assert access_control_data["finance_internal"] not in returned_ids


# ──────────────────────────────────────────────────────────────
# Integration tests — clearance 纵向隔离
# ──────────────────────────────────────────────────────────────

async def test_clearance_public_cannot_see_confidential(
    db_session: AsyncSession,
    access_control_data: dict[str, uuid.UUID],
) -> None:
    """
    场景 2：clearance=0（public）的 finance 用户看不到 confidential 文档，
    即使 acl_tags 匹配。
    """
    from app.retrieval.hybrid_search import hybrid_search

    results = await hybrid_search(
        query_vector=DUMMY_VECTOR,
        query_text="x",
        user_tags=["finance"],
        user_clearance=0,        # public 密级，只能看 sensitivity_ordinal=0
        session=db_session,
        top_k=100,
        rrf_k=60,
    )
    returned_ids = {c.chunk_id for c in results}

    assert access_control_data["finance_public"] in returned_ids, \
        "public 密级用户应能看到 finance+public 文档"
    assert access_control_data["finance_internal"] not in returned_ids, \
        "public 密级用户不应看到 internal 文档"
    assert access_control_data["finance_confidential"] not in returned_ids, \
        "public 密级用户不应看到 confidential 文档"


async def test_clearance_internal_sees_public_and_internal(
    db_session: AsyncSession,
    access_control_data: dict[str, uuid.UUID],
) -> None:
    """
    场景 3：clearance=1（internal）能看到 public + internal，看不到 confidential。
    密级是 ≤ 比较，向下兼容。
    """
    from app.retrieval.hybrid_search import hybrid_search

    results = await hybrid_search(
        query_vector=DUMMY_VECTOR,
        query_text="x",
        user_tags=["finance"],
        user_clearance=1,        # internal
        session=db_session,
        top_k=100,
        rrf_k=60,
    )
    returned_ids = {c.chunk_id for c in results}

    assert access_control_data["finance_public"] in returned_ids
    assert access_control_data["finance_internal"] in returned_ids
    assert access_control_data["finance_confidential"] not in returned_ids


async def test_clearance_confidential_sees_all_levels(
    db_session: AsyncSession,
    access_control_data: dict[str, uuid.UUID],
) -> None:
    """场景 3b：clearance=2（confidential）能看到所有密级（acl_tags 匹配时）。"""
    from app.retrieval.hybrid_search import hybrid_search

    results = await hybrid_search(
        query_vector=DUMMY_VECTOR,
        query_text="x",
        user_tags=["finance"],
        user_clearance=2,
        session=db_session,
        top_k=100,
        rrf_k=60,
    )
    returned_ids = {c.chunk_id for c in results}

    assert access_control_data["finance_public"] in returned_ids
    assert access_control_data["finance_internal"] in returned_ids
    assert access_control_data["finance_confidential"] in returned_ids
    # hr 文档仍不可见（acl_tags 不匹配）
    assert access_control_data["hr_internal"] not in returned_ids


# ──────────────────────────────────────────────────────────────
# Integration tests — "all" 全员标签
# ──────────────────────────────────────────────────────────────

async def test_all_tag_visible_to_all_users(
    db_session: AsyncSession,
    access_control_data: dict[str, uuid.UUID],
) -> None:
    """
    场景 4：仅有 ["all"] 标签的用户能看到 acl_tags=["all"] 的文档。
    这验证了 "all" 通过正常 && 运算命中，无需特例逻辑。
    """
    from app.retrieval.hybrid_search import hybrid_search

    results = await hybrid_search(
        query_vector=DUMMY_VECTOR,
        query_text="x",
        user_tags=["all"],       # 仅基础全员标签
        user_clearance=0,        # public 密级
        session=db_session,
        top_k=100,
        rrf_k=60,
    )
    returned_ids = {c.chunk_id for c in results}

    assert access_control_data["all_public"] in returned_ids, \
        "仅有 all 标签的用户应能看到 acl_tags=[all]+public 文档"
    # all_internal 密级为 internal(1)，clearance=0(public) 看不到
    assert access_control_data["all_internal"] not in returned_ids
    # finance / hr 文档不可见（acl_tags 不含 all，["all"] && ["finance"] = false）
    assert access_control_data["finance_public"] not in returned_ids
    assert access_control_data["hr_internal"] not in returned_ids


async def test_all_tag_with_internal_clearance(
    db_session: AsyncSession,
    access_control_data: dict[str, uuid.UUID],
) -> None:
    """场景 4b：clearance=1 的 all-only 用户能看到 all+public 和 all+internal。"""
    from app.retrieval.hybrid_search import hybrid_search

    results = await hybrid_search(
        query_vector=DUMMY_VECTOR,
        query_text="x",
        user_tags=["all"],
        user_clearance=1,
        session=db_session,
        top_k=100,
        rrf_k=60,
    )
    returned_ids = {c.chunk_id for c in results}

    assert access_control_data["all_public"] in returned_ids
    assert access_control_data["all_internal"] in returned_ids
    # finance/hr 专属标签文档依然不可见
    assert access_control_data["finance_internal"] not in returned_ids


# ──────────────────────────────────────────────────────────────
# Integration tests — 双标签用户
# ──────────────────────────────────────────────────────────────

async def test_user_with_multiple_tags_sees_all_matched(
    db_session: AsyncSession,
    access_control_data: dict[str, uuid.UUID],
) -> None:
    """
    场景 5：持有 ["finance","hr"] 的用户（OR 语义）能看到两个部门的文档。
    """
    from app.retrieval.hybrid_search import hybrid_search

    results = await hybrid_search(
        query_vector=DUMMY_VECTOR,
        query_text="x",
        user_tags=["finance", "hr"],
        user_clearance=1,
        session=db_session,
        top_k=100,
        rrf_k=60,
    )
    returned_ids = {c.chunk_id for c in results}

    assert access_control_data["finance_public"] in returned_ids
    assert access_control_data["finance_internal"] in returned_ids
    assert access_control_data["hr_internal"] in returned_ids
    # confidential 超出 clearance
    assert access_control_data["finance_confidential"] not in returned_ids
