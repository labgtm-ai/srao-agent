# Dockerfile — SRAO Agent for Google Cloud Run
FROM python:3.12-slim

# Install git (needed for repo cloning) and diff (for code diffs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    diffutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (Docker layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash srao && \
    chown -R srao:srao /app /tmp
USER srao

# Cloud Run listens on PORT (default 8080)
ENV PORT=8080
ENV MODE=server
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", \
     "--threads", "8", "--timeout", "3600", "main:app"]
