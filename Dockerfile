# One container for the live demo: the API replays the seed-42 match and serves the built broadcast view.

# Build the broadcast view (it bundles data/league.json).
FROM node:22-slim AS web
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
COPY data/ ../data/
RUN npm run build

# The API, serving the built site from web/dist.
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY deepkick/ deepkick/
COPY data/ data/
COPY tests/fixtures/replay_ASH-CAL-42.jsonl replays/ASH-CAL-42.jsonl
COPY --from=web /app/web/dist web/dist
# Secrets (FOUNDRY_*, AZURE_SPEECH_*) come from the Container App's secrets, never from the image.
ENV REPLAY_FILE=replays/ASH-CAL-42.jsonl REPLAY_SPEED=30
EXPOSE 8000
CMD ["uvicorn", "deepkick.api:app", "--host", "0.0.0.0", "--port", "8000"]
