#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only WITH GPL-3.0-linking-exception
# Linux/macOS counterpart of build.bat. See README.md for toolchain setup.
set -e
here=$(cd "$(dirname "$0")" && pwd)
# Load KEY=VALUE settings from .env without overriding the environment.
if [ -f "$here/.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line#export }
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%=*}
    value=${line#*=}
    case "$key" in ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;; esac
    case "$value" in \"*\") value=${value#\"}; value=${value%\"} ;; \'*\') value=${value#\'}; value=${value%\'} ;; esac
    eval "already=\${$key+set}"
    [ -n "$already" ] || export "$key=$value"
  done < "$here/.env"
fi
case "$1" in
  '')         exec python3 "$here/tools/build_firmware.py" ;;
  --web)      shift; exec python3 "$here/tools/build_web.py" "$@" ;;
  --package)  shift; exec python3 "$here/tools/build_firmware.py" --package "$@" ;;
  --help|-h)
    echo './build.sh             Build firmware'
    echo './build.sh --web       Build browser preview'
    echo './build.sh --package   Build firmware and complete workbench'
    echo 'Toolchain and output paths: README.md'
    ;;
  *) echo 'Use ./build.sh --web, ./build.sh or ./build.sh --package.' >&2; exit 2 ;;
esac
