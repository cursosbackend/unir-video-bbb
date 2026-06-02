FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard/ dashboard/

ENV CLASES_DIR=/app/clases

EXPOSE 8000

CMD ["uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8000"]
