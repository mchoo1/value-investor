#!/bin/bash
# ╔══════════════════════════════════════╗
# ║   Value Investor — Start Script      ║
# ╚══════════════════════════════════════╝

echo ""
echo "╔══════════════════════════════════════╗"
echo "║       Value Investor App             ║"
echo "║   Free · Local · No API keys        ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Go to script directory
cd "$(dirname "$0")"

# Install dependencies if needed
echo "Checking dependencies..."
pip install -r requirements.txt -q --break-system-packages 2>/dev/null || \
pip install -r requirements.txt -q 2>/dev/null

echo ""
echo "Starting server on http://localhost:5001"
echo "Press Ctrl+C to stop."
echo ""

# Open browser after short delay (macOS / Linux)
(sleep 2 && open http://localhost:5001 2>/dev/null || xdg-open http://localhost:5001 2>/dev/null) &

python app.py
