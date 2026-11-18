#!/usr/bin/env bash
# Instala tudo que o clipador precisa (Ubuntu/Debian, VPS, WSL ou nuvem).
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null; then
  echo ">> instalando ffmpeg e fontes"
  sudo apt-get update -y && sudo apt-get install -y ffmpeg fonts-dejavu-core python3-venv python3-pip
fi

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo ">> baixando modelo Whisper 'large-v3-turbo' (uma vez só, ~1.6 GB)"
python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', compute_type='int8')"

echo
echo "Pronto! Para usar:"
echo "  source .venv/bin/activate"
echo "  ./gta6.sh https://twitch.tv/CANAL"
