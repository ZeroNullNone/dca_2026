FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY server.py index.html bottom.html favicon.svg ./

EXPOSE 8765
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8765"]
