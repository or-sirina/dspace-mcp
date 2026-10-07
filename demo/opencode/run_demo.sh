#!/usr/bin/env bash
# Неинтерактивная демонстрация через `opencode run`. Запуск: ./run_demo.sh [provider/model]
set -u
cd "$(dirname "$0")"
export PATH="$PATH:$HOME/.opencode/bin"
MODEL="${1:-deepseek/deepseek-flash}"
run() { echo; echo "=== $1"; timeout 180 opencode run -m "$MODEL" "$1"; }

opencode mcp list
run "Собери SAF-пакет инструментом saf_build_from_csv из data/articles.csv (абсолютный путь от текущей директории) и сообщи item_count."
run "Вызови saf_import для профиля demo: local_saf_dir = абсолютный путь к data/SimpleArchiveFormat, collection_handle 123456789/1, confirm=false. Покажи would_run и подтверди, что ничего не выполнено."
run "Вызови dspace_ping для профиля demo и объясни результат."
