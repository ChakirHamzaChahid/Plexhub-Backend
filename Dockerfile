FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
RUN mkdir -p /app/data /app/logs
ENV PYTHONUNBUFFERED=1
ENV APP_PORT=8000
EXPOSE ${APP_PORT}
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT}"]
