FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# libgomp is LightGBM's OpenMP runtime; coinor-cbc is the LP solver PuLP shells
# out to for the allocator's dual variables.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libgomp1 coinor-cbc curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY antar ./antar
RUN pip install --upgrade pip && pip install -e ".[dev]"

COPY config ./config
COPY scripts ./scripts
COPY tests ./tests
COPY tasks.py Makefile ./

RUN mkdir -p artifacts

EXPOSE 8000 8501
CMD ["uvicorn", "antar.api:app", "--host", "0.0.0.0", "--port", "8000"]
