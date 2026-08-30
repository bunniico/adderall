FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV ADDERALL_DB=/app/data/adderall.db

# Unbuffered stdout: without it Python holds log lines in a buffer when it is
# not attached to a terminal, and `docker logs` shows nothing until the buffer
# fills. ADDERALL_LOG_LEVEL=DEBUG additionally logs the constant system prompt.
ENV PYTHONUNBUFFERED=1
ENV ADDERALL_LOG_LEVEL=INFO

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
