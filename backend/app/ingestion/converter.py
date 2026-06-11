"""
LibreOffice 无头模式文档转 PDF

职责：
  将非 PDF 文档（docx / doc / pptx / xlsx 等）转换为 PDF 字节，
  使后续解析路径能获取到真实页码（而非 python-docx 的章节序号伪页码）。

配置项（通过 Settings / .env 控制）：
  SOFFICE_PATH    — soffice 可执行文件路径；None = 自动探测（PATH + Windows 默认安装位置）
  SOFFICE_TIMEOUT — 单次转换超时秒数；默认 60s

可转换扩展名：
  CONVERTIBLE_EXTENSIONS — frozenset，方便增删，不走此集合的扩展名原样放行（.pdf/.md/.txt）

坑规避清单：
  【坑1】profile 隔离：每次调用生成唯一临时目录作为 -env:UserInstallation，
         避免与同机已运行的 LibreOffice 实例争用锁文件（会静默失败）。
  【坑2】asyncio 友好：转换运行在 asyncio.to_thread，不阻塞事件循环。
         调用方（pipeline.py）直接 await 即可。
  【坑3】超时 + kill：用 subprocess.Popen + communicate(timeout) 而非 run()；
         超时时主动 kill 进程，再 drain 输出，彻底防止 LibreOffice 卡死挂住请求。

文件名不变原则：
  函数返回 (pdf_bytes, format_override) 而非修改 filename。
  filename 由调用方（pipeline.py）保持不变地传给 parse_document(source=filename)，
  保证 chunk.source 和 citation 里显示的文件名始终是原始上传文件名（如 report.docx）。
"""

import asyncio
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import structlog

from app.config import settings

logger = structlog.get_logger()

# ──────────────────────────────────────────────────────────────
# 可转换扩展名集合（不含 .pdf 本身；方便按需增删）
# ──────────────────────────────────────────────────────────────
CONVERTIBLE_EXTENSIONS: frozenset[str] = frozenset({
    ".docx", ".doc",          # Word
    ".pptx", ".ppt",          # PowerPoint
    ".xlsx", ".xls",          # Excel
    ".odt", ".ods", ".odp",   # LibreOffice 原生格式
    ".rtf",                   # 富文本
})


# ──────────────────────────────────────────────────────────────
# 内部工具函数
# ──────────────────────────────────────────────────────────────

def _find_soffice() -> str:
    """
    自动探测 soffice 可执行文件路径，优先级：
      1. PATH 中的 soffice / soffice.exe
      2. Windows 默认安装位置
    找不到时返回 "soffice"，让 subprocess 在实际执行时抛出 FileNotFoundError
    （调用方会将其包装成清晰的用户错误）。
    """
    found = shutil.which("soffice")
    if found:
        return found

    if platform.system() == "Windows":
        for candidate in (
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ):
            if Path(candidate).exists():
                return candidate

    return "soffice"  # 回退：由 Popen 触发 FileNotFoundError


def _convert_sync(
    input_path: Path,
    output_dir: Path,
    profile_dir: Path,
    soffice_cmd: str,
    timeout: int,
) -> Path:
    """
    同步执行 soffice 转换（在 asyncio.to_thread 中调用，不阻塞事件循环）。

    Profile URI 格式：
      Path.as_uri() 在各平台均产生合法的 file:// URI：
        Linux  /tmp/xxx      → file:///tmp/xxx
        Windows C:\\tmp\\xxx → file:///C:/tmp/xxx
      LibreOffice 要求此 URI 格式作为 -env:UserInstallation 的值。

    Returns:
        转换后 PDF 文件的完整路径。

    Raises:
        RuntimeError: soffice 不存在 / 超时 / 返回非零 / 输出文件缺失。
    """
    profile_uri = profile_dir.resolve().as_uri()

    cmd = [
        soffice_cmd,
        f"-env:UserInstallation={profile_uri}",  # 【坑1】独立 profile，防止实例锁冲突
        "--headless",
        "--convert-to", "pdf",
        "--outdir", str(output_dir),
        str(input_path),
    ]

    try:
        # 【坑3】Popen + communicate(timeout) 方式：超时后主动 kill，防卡死
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()  # 排干管道，防止僵尸进程
            raise RuntimeError(
                f"LibreOffice 转换超时（>{timeout}s），文件 '{input_path.name}' 可能过大或格式异常。"
                "请检查文档是否包含宏/嵌入对象，或适当增大 SOFFICE_TIMEOUT。"
            )

    except FileNotFoundError as exc:
        cmd_display = soffice_cmd if soffice_cmd != "soffice" else "soffice（PATH）"
        raise RuntimeError(
            f"找不到 LibreOffice 可执行文件（{cmd_display!r}）。"
            "请安装 LibreOffice 并配置 SOFFICE_PATH 环境变量，"
            "或在 Dockerfile 中加入 libreoffice-writer。"
        ) from exc

    if proc.returncode != 0:
        stderr_snippet = (stderr or "").strip()[:400]
        raise RuntimeError(
            f"LibreOffice 转换失败（returncode={proc.returncode}），"
            f"文件：'{input_path.name}'。"
            f"stderr：{stderr_snippet or '(空)'}"
        )

    # soffice --outdir 会将输出文件命名为 <原文件名>.pdf
    expected_pdf = output_dir / (input_path.stem + ".pdf")
    if not expected_pdf.exists():
        # returncode=0 但输出文件缺失 = soffice 内部静默失败（格式损坏 / 许可证问题）
        raise RuntimeError(
            f"LibreOffice 报告成功但未生成 PDF 文件（预期路径：{expected_pdf.name}）。"
            "请检查 LibreOffice 安装完整性，或尝试手动用 soffice 转换该文件。"
        )

    logger.info("LibreOffice 转换成功", output=expected_pdf.name, stdout=stdout.strip()[:200])
    return expected_pdf


# ──────────────────────────────────────────────────────────────
# 公共接口
# ──────────────────────────────────────────────────────────────

async def convert_to_pdf_if_needed(
    file_bytes: bytes,
    filename: str,
) -> tuple[bytes, str | None]:
    """
    若文件扩展名在 CONVERTIBLE_EXTENSIONS 中，经 LibreOffice 无头模式转为 PDF。
    其余格式（.pdf / .md / .txt 等）原样返回，format_override=None。

    Args:
        file_bytes: 原始文件内容
        filename:   原始文件名（含扩展名，仅用于格式判断；不作为输出文件名）

    Returns:
        (bytes_for_parsing, format_override)
          · 已转换：(pdf_bytes, ".pdf")        — 调用方需用 format_override 覆盖解析器分发
          · 未转换：(original_bytes, None)      — 调用方按 filename 扩展名正常分发

    Raises:
        RuntimeError: soffice 不存在 / 转换失败 / 超时（含用户可见的中文错误提示）
        【重要】绝不静默返回原始字节作为降级——调用方必须得到真实页码或明确失败。
    """
    suffix = Path(filename).suffix.lower()

    # PDF 直接放行（原有 PDF 解析路径含 OCR，保持不变）
    if suffix == ".pdf":
        return file_bytes, None

    # 不在可转换列表（.md / .txt 等）：原样放行给现有专属解析器
    if suffix not in CONVERTIBLE_EXTENSIONS:
        return file_bytes, None

    log = logger.bind(filename=filename, suffix=suffix)
    log.info("检测到可转换格式，启动 LibreOffice 无头转换")

    # 每次调用各自独立的临时目录，防止并发转换互相干扰
    tmp_work = Path(tempfile.mkdtemp(prefix="lo_work_"))  # 存放输入文件和输出 PDF
    tmp_prof = Path(tempfile.mkdtemp(prefix="lo_prof_"))  # LibreOffice UserInstallation【坑1】

    try:
        # soffice 需要文件路径，不支持 stdin；保留原文件扩展名供 soffice 识别格式
        input_path = tmp_work / (Path(filename).stem + suffix)
        input_path.write_bytes(file_bytes)

        soffice_cmd = settings.soffice_path or _find_soffice()
        timeout = settings.soffice_timeout

        # 【坑2】在线程池中执行阻塞子进程，不阻塞 asyncio 事件循环
        pdf_path: Path = await asyncio.to_thread(
            _convert_sync,
            input_path,
            tmp_work,
            tmp_prof,
            soffice_cmd,
            timeout,
        )

        pdf_bytes = pdf_path.read_bytes()
        log.info("PDF 转换完成，进入 PDF 解析路径", pdf_size_kb=len(pdf_bytes) // 1024)

        # 返回 format_override=".pdf"；filename 由调用方保持不变（citation source 不丢失）
        return pdf_bytes, ".pdf"

    finally:
        # 【坑4】无论成功/失败/超时，清理临时目录，不泄漏临时文件
        for tmp_dir in (tmp_work, tmp_prof):
            shutil.rmtree(tmp_dir, ignore_errors=True)
