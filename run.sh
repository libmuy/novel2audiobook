#!/usr/bin/env bash
# novel2audiobook 唯一入口：./run.sh <子命令> [参数...]
#   ./run.sh status | webui | parse --novel x --chapter 0001 | test --all
# 子命令与参数原样转交 src/cli.py，详见 ./run.sh --help
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

LOCAL_CFG="config/local_config.yaml"

# 项目 venv 的 python：环境变量 > 已激活的 venv > local_config.yaml 的 tools.project_python > ./.venv
resolve_python() {
  if [[ -n "${N2A_PYTHON:-}" && -x "${N2A_PYTHON}" ]]; then echo "$N2A_PYTHON"; return; fi
  if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then echo "$VIRTUAL_ENV/bin/python"; return; fi
  if [[ -f "$LOCAL_CFG" ]]; then
    local p
    p=$(sed -n 's/^[[:space:]]*project_python:[[:space:]]*["'\'']\{0,1\}\([^"'\''#]*\).*/\1/p' "$LOCAL_CFG" | head -1)
    p="${p%"${p##*[![:space:]]}"}"
    if [[ -n "$p" && -x "$p" ]]; then echo "$p"; return; fi
  fi
  if [[ -x "./.venv/bin/python" ]]; then echo "./.venv/bin/python"; return; fi
}

PY="$(resolve_python)"
if [[ -z "$PY" ]]; then
  echo "找不到项目 venv 的 python。请在 config/local_config.yaml 里设置 tools.project_python" >&2
  echo "（模板见 config/local_config.example.yaml），或设置环境变量 N2A_PYTHON。" >&2
  exit 1
fi
exec "$PY" -m src.cli "$@"
