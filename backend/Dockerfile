# Requires Python 3.11+ (Starlette 1.x).
FROM python:3.12-slim AS base
WORKDIR /app
# Print log lines (including the first-run setup code) immediately.
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip==26.2.1 \
 && pip install --no-cache-dir -r requirements.txt

# `make test` runs the suite in this stage so every OS tests on the same Python
# the app ships with. The source and frontend are mounted at run time.
FROM base AS test
ENV PYTHONDONTWRITEBYTECODE=1
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# The app (last stage, so `docker compose build` picks it by default).
FROM base AS app
COPY main.py .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
