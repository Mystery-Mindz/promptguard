# Matches the Python version pinned for local dev/CI (3.14.x — see
# .github/workflows/backend-ci.yml and requirements.txt).
FROM python:3.14.5-slim

WORKDIR /app

# Install dependencies first, separately from app code, so this layer is
# only invalidated (and pip re-run) when requirements.txt actually changes,
# not on every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the backend. See .dockerignore for what's deliberately
# left out (venv/, caches, .env, the local SQLite db, test-data/).
COPY . .

EXPOSE 8000

# GEMINI_API_KEY (and any other config) is read from the environment at
# runtime via python-dotenv/os.getenv — never baked into the image. Pass it
# with `docker run --env-file` or `-e` (see README/CLAUDE.md for the exact
# command).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
