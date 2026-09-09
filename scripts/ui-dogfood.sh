#!/bin/bash
set -e

cd /home/adam/projects/gg

# Setup disposable repo
REPO="/tmp/gg-ui-dogfood-$(date +%s)"
mkdir -p "$REPO/src" "$REPO/tests"
cat > "$REPO/.gitignore" << 'EOF'
.venv/
__pycache__/
*.pyc
EOF
cat > "$REPO/pyproject.toml" << 'EOF'
[project]
name = "dogfood"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["pytest"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
EOF
echo "" > "$REPO/src/__init__.py"
cat > "$REPO/tests/test_dogfood.py" << 'EOF'
from src.greeting import greet
from src.farewell import farewell

def test_greet() -> None:
    assert greet() == "hello"

def test_farewell() -> None:
    assert farewell() == "goodbye"
EOF

cd "$REPO"
git init
git config user.email "dogfood@gg.local"
git config user.name "Dogfood"
git add .
git commit -m "initial"
uv sync
git add uv.lock
git commit -m "add uv.lock"
cd /home/adam/projects/gg

# Clean up any existing DB
rm -f /tmp/gg-ui-dogfood.db

# Start backend in background
PYTHONPATH=src uv run --directory backend python -m orchestrator.server &
BACKEND_PID=$!

# Wait for backend
echo "Waiting for backend..."
for i in $(seq 1 30); do
  if curl -s http://127.0.0.1:8787/api/health > /dev/null 2>&1; then
    echo "Backend ready"
    break
  fi
  sleep 1
done

# Mutating routes require the bearer token (see backend/src/orchestrator/api/auth.py).
# There is no network route to fetch it — read it straight from the file the
# backend just wrote (guaranteed present once /api/health responds, since
# create_app() generates it before uvicorn starts serving).
AUTH_TOKEN=$(cat /home/adam/projects/gg/backend/.orchestrator/auth_token)

# Create project
echo "Creating project..."
PROJECT_RESP=$(curl -s -X POST http://127.0.0.1:8787/api/projects \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -d "{\"path\":\"$REPO\",\"name\":\"ui-dogfood\"}")
PROJECT_ID=$(echo "$PROJECT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Project ID: $PROJECT_ID"

# Create and start Parallel Safe mission
echo "Creating mission..."
MISSION_RESP=$(curl -s -X POST http://127.0.0.1:8787/api/missions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -d "{\"project_id\":\"$PROJECT_ID\",\"title\":\"UI Dogfood Parallel\",\"task\":\"Create greeting and farewell modules\",\"autonomy\":\"BALANCED\",\"profile\":\"balanced\",\"scheduling_mode\":\"PARALLEL_SAFE\",\"start\":true}")
MISSION_ID=$(echo "$MISSION_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Mission ID: $MISSION_ID"

# Start frontend in background
cd /home/adam/projects/gg/frontend
npm run dev -- --port 5173 &
FRONTEND_PID=$!

# Wait for frontend
echo "Waiting for frontend..."
for i in $(seq 1 30); do
  if curl -s http://127.0.0.1:5173 > /dev/null 2>&1; then
    echo "Frontend ready"
    break
  fi
  sleep 1
done

# Run Playwright screenshot script from frontend dir
cd /home/adam/projects/gg/frontend
node -e "
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const shots = '/tmp/gg-ui-dogfood-shots';
fs.mkdirSync(shots, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.goto('http://127.0.0.1:5173/#/');
  await page.waitForTimeout(2000);
  await page.screenshot({ path: path.join(shots, '00-initial.png'), fullPage: true });

  for (let i = 1; i <= 12; i++) {
    await page.waitForTimeout(5000);
    await page.reload();
    await page.waitForTimeout(1000);
    await page.screenshot({ path: path.join(shots, String(i).padStart(2, '0') + '.png'), fullPage: true });
  }

  await browser.close();
  console.log('Screenshots saved to', shots);
})();
"

# Get final mission state
echo "Final mission state:"
curl -s "http://127.0.0.1:8787/api/missions/$MISSION_ID" | python3 -m json.tool

# Cleanup
kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true
wait $BACKEND_PID $FRONTEND_PID 2>/dev/null || true

echo "Dogfood complete. Screenshots in /tmp/gg-ui-dogfood-shots"
