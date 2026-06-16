#!/usr/bin/env bash
set -e

echo "============================================================"
echo " Site Inspection Photo Processor — macOS Setup"
echo "============================================================"
echo

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install from https://python.org or via Homebrew:"
    echo "  brew install python"
    exit 1
fi

echo "[1/3] Creating virtual environment..."
python3 -m venv venv

echo "[2/3] Installing dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "[3/3] Creating launch script..."
cat > run.sh << 'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
source venv/bin/activate
python main.py
EOF
chmod +x run.sh

echo
echo "============================================================"
echo " Setup complete!"
echo " Run:  ./run.sh"
echo "============================================================"
