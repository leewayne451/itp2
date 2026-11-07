FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# A batch worker: no port is exposed and it runs as an unprivileged user.
RUN useradd --system --no-create-home analytics
USER analytics

CMD ["python", "pipeline.py"]
