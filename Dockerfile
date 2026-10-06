FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.29" "sqlalchemy>=2.0" "pydantic>=2.6" \
    "pydantic-settings>=2.2" "httpx>=0.27" "python-multipart>=0.0.9" "psycopg[binary]>=3.1" "redis>=5" "jinja2>=3.1" "itsdangerous>=2.1" "openpyxl>=3.1"
COPY . .
CMD ["uvicorn", "sofa.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
