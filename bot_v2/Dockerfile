FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip

COPY . .
RUN pip install --no-cache-dir .

# Liveness reflects the atomic runtime health file, not network reachability.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -m scripts.healthcheck --kind liveness

# Operator dashboard process. Startup performs lease-based auto-resume;
# live authorization is never taken from the command line.
CMD ["python", "-m", "dashboard.main", "--host", "127.0.0.1", "--port", "8000"]
