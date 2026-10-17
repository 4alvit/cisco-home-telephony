# Презентация: Cisco 7945G, SIP и домашний k3s

[Открыть PDF](cisco-home-telephony.pdf) · [Скачать редактируемый PowerPoint](cisco-home-telephony.pptx)

12 слайдов на русском: купленная партия из десяти 7945G, домашняя сетевая схема,
PoE, переход SCCP/SIP, TFTP, Asterisk PJSIP, развёртывание и проверка звонков,
резервные копии и запуск всей партии. Подписи и технические схемы в PPTX состоят
из редактируемых объектов. Ссылки на первичные источники находятся в заметках
соответствующих слайдов. PDF предназначен для чтения и печати.

Презентация сопровождает [подробную инструкцию k3s](../k3s-deployment.md) и
[настройку телефонов](../provisioning.md). Перед прошивкой проверьте текущую
App Load и аппаратную ревизию каждого телефона. Для HW 13.0+ нельзя переходить
ниже 9.3(1)SR3. Порядок обновления определяется release notes выбранной версии.

## Исходники

- [tools/build_slides.mjs](../../tools/build_slides.mjs) содержит текст, цвета,
  расположение объектов, заметки со ссылками и данные для трёх SVG.
- [SVG-схемы](../diagrams/) можно редактировать в текстовом редакторе, Inkscape
  или другом редакторе SVG. Они не содержат внешних изображений и шрифтов.
- Шрифт презентации: Arial. Формат: 16:9, 1280 × 720.

## Пересборка SVG без специальных пакетов

Из корня репозитория:

```bash
node tools/build_slides.mjs --svg-only
```

Команда обновляет `docs/diagrams/architecture.svg`, `commissioning.svg` и
`power.svg`. Достаточно современного Node.js, сетевые запросы не выполняются.

## Пересборка PPTX и PDF

Авторинг использует JavaScript-пакет `@oai/artifact-tool` из bundled workspace
runtime Codex. Это **не публичная npm-зависимость**: не устанавливайте случайный
одноимённый пакет. В Codex сначала получите пути через инструмент
`load_workspace_dependencies`, затем задайте переменные своими путями:

```bash
export RUNTIME_NODE=/path/to/bundled/node
export RUNTIME_NODE_MODULES=/path/to/bundled/node_modules
export ARTIFACT_TOOL_ROOT="$RUNTIME_NODE_MODULES/@oai/artifact-tool"
export SOFFICE=/path/to/bundled/soffice
"$RUNTIME_NODE" tools/build_slides.mjs
```

Для встроенной проверки пакета перед выдачей файла дополнительно задайте:

```bash
export PRESENTATION_SKILL_DIR=/path/to/presentations/skills/presentations
export RUNTIME_PYTHON=/path/to/bundled/python3
"$RUNTIME_NODE" tools/build_slides.mjs
```

Сборка записывает PPTX и PDF в `docs/slides/`. Изображения для визуальной проверки,
кандидат PPTX и отчёты проверок сохраняются в игнорируемой `.slides-build/`.
Если встроенный PDF-экспорт недоступен в установленном runtime, скрипт использует
LibreOffice через `SOFFICE`. Просмотрите все слайды после правок: структурная
проверка не подтверждает, что надписи и стрелки читаются правильно.

Для обычного редактирования готового PPTX в PowerPoint или LibreOffice этот
runtime не нужен.
