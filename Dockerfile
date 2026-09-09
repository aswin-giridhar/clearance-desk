FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY samples ./samples
# Ship a warm cache so the demo works even if the shared cluster quota is spent.
COPY cache.json ./cache.json
# ADK builds its own genai client; without this it defaults to the
# Gemini Developer API and fails with "No API key was provided".
ENV GOOGLE_GENAI_USE_VERTEXAI=TRUE
ENV PORT=8080
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 120
