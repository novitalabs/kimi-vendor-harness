#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <run-contract.json> <file-or-directory>..." >&2
  exit 2
fi

contract=$1
shift
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$script_dir/validate-run-contract.sh" "$contract" >/dev/null

for tool in base64 jq mktemp rg tar; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool is required" >&2
    exit 2
  fi
done

audit_tmp=$(mktemp -d /tmp/kbench-secret-audit.XXXXXX)
trap 'rm -rf -- "$audit_tmp"' EXIT
needles=$audit_tmp/needles.txt
: > "$needles"

auth_kind=$(jq -r '.authentication.kind' "$contract")
auth_env=$(jq -r '.authentication.env_name // empty' "$contract")

if [ "$auth_kind" = "env" ]; then
  auth_value=${!auth_env:-}
  if [ "${#auth_value}" -lt 8 ]; then
    echo "authentication environment variable is unset or too short: $auth_env" >&2
    exit 2
  fi
  printf '%s\n' "$auth_value" >> "$needles"
  printf '%s' "$auth_value" | base64 | tr -d '\n' >> "$needles"
  printf '\n' >> "$needles"
elif [ "$auth_kind" = "oauth_file" ]; then
  oauth_path=${!auth_env:-}
  if [ -z "$oauth_path" ] || [ ! -f "$oauth_path" ]; then
    echo "OAuth credential path environment variable is unset or invalid: $auth_env" >&2
    exit 2
  fi
  jq -r '
    .. | objects | to_entries[]
    | select(.key | test("token|secret|password|api[_-]?key"; "i"))
    | .value | strings | select(length >= 8)
  ' "$oauth_path" >> "$needles"
  base64 < "$oauth_path" | tr -d '\n' >> "$needles"
  printf '\n' >> "$needles"
fi

sort -u "$needles" -o "$needles"
scan_root=$audit_tmp/expanded
mkdir -p "$scan_root"
archive_index=0

for target in "$@"; do
  if [ ! -e "$target" ]; then
    echo "audit target not found: $target" >&2
    exit 2
  fi
  if [ -f "$target" ] && [[ "$target" == *.tar.gz || "$target" == *.tgz ]]; then
    archive_index=$((archive_index + 1))
    archive_dir=$scan_root/archive-$archive_index
    mkdir -p "$archive_dir"
    tar xzf "$target" -C "$archive_dir"
  elif [ -d "$target" ]; then
    while IFS= read -r -d '' archive; do
      archive_index=$((archive_index + 1))
      archive_dir=$scan_root/archive-$archive_index
      mkdir -p "$archive_dir"
      tar xzf "$archive" -C "$archive_dir"
    done < <(find "$target" -type f \( -name '*.tar.gz' -o -name '*.tgz' \) -print0)
  fi
done

leak_found=0
while IFS= read -r needle; do
  [ -z "$needle" ] && continue
  for target in "$@" "$scan_root"; do
    if [ -f "$target" ] && [[ "$target" == *.tar.gz || "$target" == *.tgz ]]; then
      continue
    fi
    if [ -e "$target" ] && rg -l --hidden --no-messages -F \
      --glob '!*.tar.gz' --glob '!*.tgz' -- "$needle" "$target" >/dev/null 2>&1; then
      echo "credential material found in audit target: $target" >&2
      leak_found=1
    fi
  done
done < "$needles"

if [ "$leak_found" -ne 0 ]; then
  echo "secret_audit=failed" >&2
  exit 1
fi

printf 'secret_audit=passed\n'
printf 'authentication_source=%s%s\n' "$auth_kind" "${auth_env:+:$auth_env}"
printf 'targets_scanned=%s\n' "$#"
