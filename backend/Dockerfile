FROM python:3.11-slim

# 安装系统依赖
#   tesseract-ocr: OCR 引擎本体
#   tesseract-ocr-chi-sim / eng: 中英文语言包
#   libreoffice-writer: 非 PDF 文档（docx/doc/pptx/xlsx 等）转 PDF，获取真实页码
#     使用 libreoffice-writer 而非 libreoffice（全套），体积更小；
#     如需 Excel 或 Impress 转换支持，可改为 libreoffice-calc / libreoffice-impress
#   curl: 健康检查探针
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-eng \
    libreoffice-writer \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖定义，利用 Docker 层缓存
COPY pyproject.toml .
RUN pip install --no-cache-dir -e "."

COPY . .

# 单 worker 是硬性要求：
#   bge-m3 约 2.3GB、reranker 约 568MB，多 worker 各自加载副本内存直接耗尽。
#   生产横向扩展通过增加 Pod/容器数量实现，而非多 worker。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
