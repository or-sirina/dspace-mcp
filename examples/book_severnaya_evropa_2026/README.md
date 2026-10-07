# Пример: депонирование сборника конференции в DSpace

**Книга:** «Страны Северной Европы и Балтии : общество, культура, язык» —
материалы II Межвузовской студенческой научно-практической конференции
(МГИМО, 8 апреля 2026 г.). ISBN 978-5-9228-3244-1, отв. ред. и сост.
Д. Н. Солдатова, 202 [1] с., **78 докладов** в 7 секциях.

Сборник депонируется двумя способами одновременно:

- **целиком** — 1 item в коллекции 123456789/8577 «Сборники конференций»;
- **постатейно** — 78 items в отдельной коллекции II конференции в сообществе
  123456789/136 «Конференции» (у I конференции это коллекция 123456789/23969).

Пакеты для этого депонирования были собраны набором скриптов `xml_to_saf`,
из которого вырос `dspace-mcp`. Здесь тот же депозит **воспроизводится
инструментами dspace-mcp** и сверяется с оригиналом, item за item'ом.

> **Статус:** по рабочим заметкам на 10.08.2026 пакеты лежали на сервере в
> `/tmp/sev2026`, а импорт ждал запуска от root. Все шаги ниже, которые
> что-то меняют на сервере, показаны как **сухой прогон** (`confirm=False`) на
> демо-профиле с недостижимым хостом. На живой репозиторий они не ходят.

## Файлы

| Файл | Что это |
|---|---|
| `volume.csv` | манифест сборника целиком: 1 строка |
| `articles.csv` | манифест 78 докладов: метаданные на двух языках, ключевые слова, аннотации, страницы в цитировании |
| `make_manifests.py` | восстанавливает эти CSV из готовых SAF-пакетов депозита |
| `run_example.py` | прогоняет весь сценарий через MCP-сервер по stdio |
| `run_output.txt` | реальный вывод последнего прогона |

PDF в репозиторий не входят. Скрипты берут их из каталога депозита: переменная
`SEV2026_SRC`, по умолчанию `xml_to_saf/deposits/severnaya_evropa_2026`.

## Запуск

```bash
uv run python examples/book_severnaya_evropa_2026/run_example.py
```

## Процесс по шагам

### Шаг 0. Из PDF — в метаданные (подготовка, вне MCP)

Исходник — один PDF на 204 страницы. Его разбор на доклады — задача,
специфичная для конкретного издания, поэтому она решена скриптами депозита,
а не общим инструментом:

1. `parse_sbornik.py` — `pdftotext`, поиск строк с авторами, заголовков
   прописными, блоков «Аннотация / Abstract» и «Ключевые слова / Keywords»,
   границ статей (стр. 9–203 без пропусков и наложений);
2. `normalize.py` — приведение заголовков к обычному регистру по словарю из
   текста самого сборника, авторы в виде «Фамилия, И. О.», сверка оглавления
   с шапками статей (нашлись 4 соавтора, которых нет в оглавлении, и 2 расхождения в инициалах);
3. `metadata_review.csv` — таблица для вычитки редактором до импорта.

В dspace-mcp этому этапу помогают `extract_pdf_text` (текст PDF),
`enrich_metadata_llm` (переводы заголовков, ключевые слова) и
`normalize_authors`. Итог этапа — манифест CSV в формате
`saf_build_from_csv`:

```
filename__bundle:ORIGINAL, dc.title[ru], dc.title.alternative[en],
dc.contributor.author, dc.contributor.editor, dc.date.issued,
dc.publisher[ru], dc.language.iso, dc.identifier.isbn,
dc.identifier.citation[ru], dc.relation.ispartof[ru],
dc.relation.ispartofseries, dc.description.abstract[ru],
dc.description.abstract[en], dc.subject[ru], dc.subject[en],
dc.type[en], dc.type[ru], local.type.coar, collection
```

Несколько значений в одной ячейке разделяются `||`, например
`датский язык||семантика||эмотивы`. Тип по словарю COAR: для докладов
`c_5794` (conference paper), для сборника `c_2f33` (book).

### Шаг 1. Сборка SAF для сборника — `saf_build_from_csv`

```json
{"status":"ok","saf_dir":"<work>/saf_volume","item_count":1,"warnings":[]}
```

### Шаг 2. Сборка SAF для 78 докладов — `saf_build_from_csv`

```json
{"status":"ok","saf_dir":"<work>/saf_articles","item_count":78,"warnings":[]}
```

Для каждого доклада создаются `dublin_core.xml`, `metadata_local.xml`
(`local.type.coar`), `contents` и PDF доклада.

### Шаг 3. Проверка пакетов — `saf_validate`

```json
{"status":"ok","issue_count":0,"issues":[]}   // saf_volume
{"status":"ok","issue_count":0,"issues":[]}   // saf_articles
```

### Шаг 4. Сверка с оригинальным депозитом

Метаданные каждого item'а сравниваются как множество
(schema, element, qualifier, language, value); `contents` сравнивается тоже.

```
saf_volume:   идентичных 1/1
saf_articles: идентичных 78/78
```

Итог: dspace-mcp собирает **тот же самый** депозит, что и исходные скрипты.

### Шаг 5. Коллекция под II конференцию — `collection_create`

Без явного разрешения на запись в БД инструмент отказывается работать:

```json
{"status":"blocked","executed":false,"reason":"collection_create writes directly
 to the database/assetstore. Retry with allow_db_write=True; a fresh backup will
 be taken automatically first."}
```

В боевом прогоне: `allow_db_write=True, confirm=True`. Сначала автоматически
делается `db_backup`, при сбое резервной копии запись не выполняется. Раньше
для этого был отдельный `create_collection.sql`.

### Шаг 6. Импорт — `saf_import` (сухой прогон)

Доклады (вместо `123456789/99999` подставьте handle коллекции из шага 5):

```
scp <work>/saf_articles.zip -> /tmp/dspace_mcp_saf_saf_articles.zip
rm -rf /tmp/dspace_mcp_saf_saf_articles && mkdir -p … && unzip -oq … -d /tmp/dspace_mcp_saf_saf_articles
sudo dspace import -a -e admin@example.org -c 123456789/99999 -s /tmp/dspace_mcp_saf_saf_articles/saf_articles -m /tmp/dspace_mcp_mapfile_saf_articles.txt
chown -R tomcat8:tomcat8 /dspace/assetstore
→ Imports 78 item(s) into collection 123456789/99999.
```

Сборник целиком:

```
sudo dspace import -a -e admin@example.org -c 123456789/8577 -s /tmp/dspace_mcp_saf_saf_volume/saf_volume -m …
→ Imports 1 item(s) into collection 123456789/8577.
```

Ничего не выполнено (`"executed": false`). Для настоящего импорта тот же вызов
повторяется с `confirm=True`.

### Шаг 7. Полный текст и поисковый индекс

`filter_media` (сухой прогон):

```
dspace filter-media -i 123456789/99999 -p 'PDF Text Extractor'
```

`index_discovery` в примере не вызывается: у инструмента нет параметра
`confirm`, и он сразу выполняется на сервере. Переиндексация не меняет данных,
но на демо-хосте вызов просто ждал бы таймаута SSH.

## Было → стало

| Шаг | Исходный депозит (`xml_to_saf`) | dspace-mcp |
|---|---|---|
| Разбор PDF | `parse_sbornik.py`, `normalize.py` | скрипты издания + `extract_pdf_text`, `enrich_metadata_llm`, `normalize_authors` |
| Сборка SAF | `build_saf.py` | `saf_build_from_csv` |
| Проверка | вручную | `saf_validate`, `saf_repair` |
| Коллекция | `create_collection.sql` под root | `collection_create` с `allow_db_write` и автоматической резервной копией |
| Импорт | `import.sh`: `dspace import -t`, затем пауза и импорт | `saf_import`: сухой прогон, затем `confirm=True` |
| Полный текст | `dspace filter-media` | `filter_media` |
| Индекс | `dspace index-discovery` | `index_discovery` |
| Пароли | `PGPASSWORD` прямо в скрипте | `env:VAR` в конфиге, пароли скрыты из вывода |

## Запросы для агента (opencode)

1. «Собери SAF из examples/book_severnaya_evropa_2026/work/articles.csv и проверь пакет»
2. «Сравни количество items с оглавлением сборника: должно быть 78»
3. «Покажи, что будет выполнено при импорте докладов в коллекцию 123456789/99999. Ничего не запускай»
4. «Какие шаги останутся после импорта, чтобы статьи появились в поиске с полным текстом?»
