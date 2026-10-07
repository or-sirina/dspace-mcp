# Демонстрация dspace-mcp в opencode

MCP-сервер `dspace-mcp` подключается к opencode как локальный stdio-сервер `dspace`
(инструменты в opencode получают префикс `dspace_`, например `dspace_saf_build_from_csv`).
Подключение к DSpace в демо вымышленное: профиль `demo`, хост `203.0.113.10` (недостижим, RFC 5737), секретов нет.

## Что нужно заранее

- Python >= 3.10, [`uv`](https://docs.astral.sh/uv/), установленный пакет: из корня репозитория
  `uv venv .venv && uv pip install -e . --python .venv/bin/python` (mcp должен быть версии 1.x, `mcp<2`).
- opencode >= 1.18 (`~/.opencode/bin/opencode`) и настроенный провайдер LLM (`opencode auth list`; проверено с `deepseek/deepseek-flash`).
- Интернет нужен только для шага 5 (OpenAlex/CrossRef/Unpaywall). Остальные шаги работают офлайн.

Файлы: `opencode.json` (регистрация сервера), `config.demo.toml` (профиль-пустышка, путь передаётся через
`DSPACE_MCP_CONFIG`), `data/articles.csv` (3 статьи: рус+англ), `run_demo.sh`, `smoke_test.py` (проверка без LLM).

## Запуск

```bash
cd demo/opencode
opencode mcp list     # ожидается: dspace connected
opencode              # интерактивный режим; выбрать модель командой /models при необходимости
```

Проверка без модели: `uv --directory ../.. run python demo/opencode/smoke_test.py`.

## Сценарий (вводить в opencode)

1. **Список инструментов.**
   `Какие инструменты есть у MCP-сервера dspace? Сгруппируй по назначению, коротко.`
   Ожидается: ответ по описаниям инструментов (вызова может не быть; список видно по `/mcp`). Аудитория видит 33 инструмента:
   сборка SAF, импорт, правка метаданных, нормализация, наукометрия, OA-PDF, диагностика.

2. **Локальная сборка SAF-пакета.**
   `Собери SAF-пакет из файла data/articles.csv с помощью saf_build_from_csv и покажи итог.`
   Вызов: `dspace_saf_build_from_csv`. Результат: `status: ok`, `item_count: 3`, каталог `data/SimpleArchiveFormat/item_001..003`
   с `dublin_core.xml`, `contents`, `collections`. Можно открыть любой `dublin_core.xml`: видны `language="ru"/"en"`.

3. **Валидация пакета.**
   `Проверь собранный пакет data/SimpleArchiveFormat инструментом saf_validate.`
   Вызов: `dspace_saf_validate`. Результат: список проблем пуст.

4. **Сухой запуск импорта (защита от записи).**
   `Импортируй пакет data/SimpleArchiveFormat в коллекцию 123456789/1 через saf_import, профиль demo. Подтверждение не давай.`
   Вызов: `dspace_saf_import` с `confirm=false`. Результат: `status: dry_run`, `executed: false`, точные команды
   (`scp`, `unzip`, `sudo dspace import -a ...`, `chown`), `item_count: 3`. Ничего не выполняется и в сеть не ходит.
   Скажите аудитории: выполнить можно только явным `confirm=true`.

5. **Наукометрический сбор (нужен интернет).**
   `Найди через harvest_by_affiliation в OpenAlex до 5 публикаций по аффилиации "MGIMO", mailto demo@example.org, и покажи названия и DOI.`
   Вызов: `dspace_harvest_by_affiliation` (sources=["openalex"], `max_results_per_source=5`). Результат: строки в общей схеме
   (title, doi, year, OA-ссылка). Без интернета вернётся ошибка; это нормально для офлайн-показа.
   Для OA-поиска по DOI можно продолжить: `Скачай OA PDF для этих строк в папку data/pdf через download_oa_pdfs, unpaywall_email demo@example.org, limit 2.`

6. **Корректная ошибка при недоступном сервере.**
   `Проверь связь с репозиторием: dspace_ping, профиль demo.`
   Вызов: `dspace_ping`. Результат: не падение, а структурированный `status: error` с причиной
   (таймаут `ssh ... demo@203.0.113.10`, около 15 секунд). Агент объясняет, что хост недоступен.

## Очистка

`rm -rf data/SimpleArchiveFormat data/SimpleArchiveFormat.zip data/pdf` (эти пути игнорируются git).
