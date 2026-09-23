#!/usr/bin/env bash
# 激活 novel2audiobook 项目的虚拟环境
# 用法: source src/scripts/activate.sh
# venv 路径取自 config/local_config.yaml 的 tools.project_python（日常用 ./run.sh 则不必激活）

_n2a_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
_n2a_py="${N2A_PYTHON:-}"
if [[ -z "$_n2a_py" && -f "$_n2a_root/config/local_config.yaml" ]]; then
  _n2a_py=$(sed -n 's/^[[:space:]]*project_python:[[:space:]]*["'\'']\{0,1\}\([^"'\''#]*\).*/\1/p' "$_n2a_root/config/local_config.yaml" | head -1)
  _n2a_py="${_n2a_py%"${_n2a_py##*[![:space:]]}"}"
fi

if [[ -n "$_n2a_py" && -f "$(dirname "$_n2a_py")/activate" ]]; then
  # shellcheck disable=SC1091
  source "$(dirname "$_n2a_py")/activate"
  unset _n2a_root _n2a_py
else
  echo "找不到 venv：请在 config/local_config.yaml 里设置 tools.project_python（模板见 config/local_config.example.yaml）" >&2
  unset _n2a_root _n2a_py
  return 1
fi
