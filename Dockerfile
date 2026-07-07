FROM python:3.11-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
# Install Chromium and its OS dependencies for Playwright
RUN playwright install chromium --with-deps

COPY . .

EXPOSE 5000

# --workers must stay 1: the in-process APScheduler would double-fire jobs
# with multiple workers. Scale with --threads instead (AI calls block threads).
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 8 --timeout 120 --worker-tmp-dir /dev/shm"]
