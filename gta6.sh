#!/usr/bin/env bash
# Clipador pronto para as lives de GTA 6.
# Uso:  ./gta6.sh https://twitch.tv/CANAL1 https://kick.com/CANAL2 [opções extras]
# Ex.:  ./gta6.sh https://twitch.tv/cellbit --formatos tiktok,shorts
cd "$(dirname "$0")"
[ -d .venv ] && . .venv/bin/activate
# (as opções que você passar depois das URLs substituem estes padrões)
exec python3 -m clipador ao-vivo \
  --perfil gta6 \
  --formatos tiktok \
  --layout desfoque \
  --sensibilidade 2.5 \
  --intervalo 60 \
  "$@"
