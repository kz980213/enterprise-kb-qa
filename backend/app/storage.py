"""
存储后端抽象（StorageBackend Protocol + NoopStorage 实现）

设计原则：
  · 入库流水线调用 storage.save(file_bytes, filename) 获取对象存储 key；
    key 写入 documents.storage_key 列，供将来下载端点使用。
  · demo 阶段使用 NoopStorage：save() 不写任何东西，返回 None；
    documents.storage_key 保持 NULL，表示"文件未持久化"。
  · 将来切换真实后端（S3/MinIO/OSS）：实现同一 Protocol，
    修改 STORAGE_BACKEND 环境变量即可，pipeline.py / routes 零修改。

接口方法：
  save(file_bytes, filename) → str | None
    保存文件字节；成功返回存储 key，NoopStorage 返回 None。
  get_url(key) → str
    生成下载 URL（预签名或直链），供下载端点使用。
  delete(key) → None
    删除存储后端中对应 key 的文件（文档删除时调用）。

切换存储后端（设置环境变量）：
  STORAGE_BACKEND=noop   → NoopStorage（默认，demo 用）
  STORAGE_BACKEND=local  → 将来加：LocalDiskStorage（开发/测试）
  STORAGE_BACKEND=s3     → 将来加：S3Storage（生产）
"""

import os
from typing import Protocol, runtime_checkable

import structlog

logger = structlog.get_logger()


# ──────────────────────────────────────────────────────────────
# Protocol 接口
# ──────────────────────────────────────────────────────────────

@runtime_checkable
class StorageBackend(Protocol):
    """对象存储抽象接口，调用方依赖此协议而非具体实现。"""

    def save(self, file_bytes: bytes, filename: str) -> str | None:
        """
        保存文件字节，返回存储 key；如不支持持久化则返回 None。

        key 写入 documents.storage_key 列，供下载端点的 get_url() 使用。
        IO 密集型实现（S3/OSS）建议在内部使用 asyncio.to_thread 包装，
        或在升级为 async Protocol 时迁移。
        """
        ...

    def get_url(self, key: str) -> str:
        """根据 key 生成下载 URL（预签名 URL 或直链）。"""
        ...

    def delete(self, key: str) -> None:
        """删除存储后端中对应 key 的文件（文档删除时调用）。"""
        ...


# ──────────────────────────────────────────────────────────────
# NoopStorage（demo 占位实现）
# ──────────────────────────────────────────────────────────────

class NoopStorage:
    """
    Demo 阶段占位存储：save() 不写任何东西，返回 None。

    documents.storage_key 将为 NULL——将来切换真实后端后，
    重新入库即可填充 key；key=NULL 表示"源文件未持久化"。
    """

    def save(self, file_bytes: bytes, filename: str) -> str | None:
        logger.debug(
            "NoopStorage.save（demo 模式，源文件不持久化）",
            filename=filename,
            size=len(file_bytes),
        )
        return None

    def get_url(self, key: str) -> str:
        return ""

    def delete(self, key: str) -> None:
        pass


# ──────────────────────────────────────────────────────────────
# 工厂函数
# ──────────────────────────────────────────────────────────────

_storage: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """
    返回存储后端单例（按 STORAGE_BACKEND 环境变量选择实现）。

    当前支持：
      noop（默认）→ NoopStorage

    将来扩展时新增分支即可，调用方代码无需改动。
    """
    global _storage
    if _storage is None:
        backend = os.getenv("STORAGE_BACKEND", "noop").lower()
        if backend == "noop":
            _storage = NoopStorage()
            logger.info("存储后端初始化", backend="noop")
        else:
            raise ValueError(
                f"未知的 STORAGE_BACKEND='{backend}'。"
                "当前支持的值：noop。"
            )
    return _storage
