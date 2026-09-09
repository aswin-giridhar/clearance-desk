FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY samples ./samples
ENV PORT=8080
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 120
