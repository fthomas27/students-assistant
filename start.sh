#!/bin/bash
set -e

echo "Starting Schola Registry..."

# 1. Ensure PostgreSQL is running
echo "Checking PostgreSQL..."
sudo service postgresql start 2>/dev/null || sudo systemctl start postgresql 2>/dev/null || true
sleep 2

# 2. Create PostgreSQL user and database if they don't exist
echo "Setting up PostgreSQL user and database..."
sudo -u postgres psql -c "CREATE USER root WITH SUPERUSER CREATEDB CREATEROLE LOGIN PASSWORD 'root';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE student_assistant OWNER root;" 2>/dev/null || true

# 3. Install dependencies from the pinned list, so this can't drift
echo "Installing dependencies..."
pip install --ignore-installed --break-system-packages -q -r requirements.txt

# 4. Start the app with proper environment
echo "Starting Flask app..."
cd "$(dirname "$0")"
export DATABASE_URL="${DATABASE_URL:-postgresql://root:root@localhost/student_assistant}"
export FLASK_ENV=production
export FLASK_APP=app.py
export SECURITY_CODE="${SECURITY_CODE:-}"
export PORT="${PORT:-8000}"

pkill -f "python app.py" || true
sleep 1

python app.py
