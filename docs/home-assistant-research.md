# Home Assistant: проверенные возможности и контракт интеграции

Проверено 16 октября 2026 года (Pacific). Здесь различаются проверка исходного
кода, испытание контейнера и ещё не выполненная приёмка физических телефонов.

## Выбор компонентов

Для управления PBX используем существующую
[TECH7Fox/asterisk-hass-integration](https://github.com/TECH7Fox/asterisk-hass-integration),
а для разговора с Assist — встроенную
[Home Assistant Voice over IP](https://www.home-assistant.io/integrations/voip/).
Это разные соединения: HA подключается к AMI Asterisk по TCP; Asterisk отправляет
вызов Assist на SIP/UDP HA. Cisco регистрируются только на Asterisk и продолжают
использовать SIP/TCP и G.711. Новая пользовательская HA-интеграция для этих
базовых функций не требуется.

Проверенная версия TECH7Fox — `v1.0.5`, commit
`65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e`. Tag указывает именно на этот commit;
[release](https://github.com/TECH7Fox/asterisk-hass-integration/releases/tag/v1.0.5)
опубликован 15 апреля 2026. Обнаружение endpoint, обработчики датчиков и service
проверены по коду, а не только по списку возможностей на сайте.

Архив для воспроизводимой установки:

```text
https://github.com/TECH7Fox/asterisk-hass-integration/archive/65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e.zip
SHA-256: 697792a522eb90db62672ff57bc0b03bec4caba45c0748583bdd32768e6dcad1
```

Обычная установка описана через HACS. В проверенном дереве upstream нет LICENSE;
его исходники не следует копировать в наш публичный companion-репозиторий как
собственную распространяемую интеграцию. Инсталлятор может получать выбранный
архив непосредственно из upstream. Зависимость `asterisk-ami>=0.1.7` у upstream
не закреплена точной версией; pin исходников интеграции не закрепляет её транзитивные
Python-зависимости.

## Что действительно предоставляет TECH7Fox

- Автоматически обнаруживает PJSIP endpoint через `PJSIPShowEndpoints`.
  Отсутствие устаревшего `chan_sip` допустимо: ошибка `SIPpeers` обработана.
- Серверный binary sensor `AMI Connected` отражает связь с AMI.
- Для каждого endpoint есть `Registered`, `State`, `Connected Line`,
  `DTMF Sent`, `DTMF Received`.
- `Registered` вычисляется из DeviceState, а не из SIP ContactStatus или
  отдельного подтверждения регистрации. `State` показывает использование линии,
  звонок, удержание и недоступность.
- `Connected Line` содержит номер и атрибуты `Channel`, `ChannelState`,
  `ChannelStateDesc`, `CallerIDNum`, `CallerIDName`, `ConnectedLineNum`,
  `ConnectedLineName`, `Exten`, `Context`; после Hangup добавляются причина и код.
  Это последний обработанный канал, не полный журнал или счётчик всех звонков.
- `asterisk.send_action` отправляет произвольный AMI action с параметрами.
  Отдельных services `asterisk.originate` и `asterisk.hangup` нет.

Источники:
[setup и send_action](https://github.com/TECH7Fox/asterisk-hass-integration/blob/65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e/custom_components/asterisk/__init__.py),
[датчики](https://github.com/TECH7Fox/asterisk-hass-integration/blob/65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e/custom_components/asterisk/sensor.py),
[binary sensors](https://github.com/TECH7Fox/asterisk-hass-integration/blob/65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e/custom_components/asterisk/binary_sensor.py).

Схема предназначена для **одного AMI server**. Service регистрируется глобально,
не имеет выбора config entry и при нескольких серверах может обращаться к
последнему зарегистрированному клиенту. После добавления endpoint в Asterisk
перезагрузить интеграцию для повторного discovery. После потери AMI связи нельзя
считать сохранённое значение endpoint доказательством его текущего состояния:
проверять также `AMI Connected`. Upstream не восстанавливает полную историю
событий за время разрыва. Совместимость именно с установленной версией HA требует
локальной приёмки; upstream выполняет синхронные AMI операции из async setup.

## AMI: минимальные классы прав для выбранных функций

Поля config flow: `host`, `port` (по умолчанию `5038`), `username`, `password`.
В этой версии поля TLS нет. Использовать отдельную учётную запись, доступ только
от фактического IP процесса HA, закрытую домашнюю сеть, firewall и AMI ACL.
Не открывать AMI всему phone LAN или публичному Интернету.

Минимум для discovery, датчиков, Originate и Hangup на Asterisk 22.11.0:

```ini
[general]
enabled = yes
webenabled = no
bindaddr = PBX_LAN_IP
port = 5038

[homeassistant]
secret = GENERATED_PRIVATE_AMI_SECRET
deny = 0.0.0.0/0.0.0.0
permit = HA_SOURCE_IP/255.255.255.255
read = call,dtmf
write = system,originate
```

Это шаблон: заменить оба IP и secret; не публиковать получившийся файл.
`PJSIPShowEndpoints` требует `system`; `CoreSettings` допускает `system` или
`reporting`; `Hangup` допускает `system` или `call`; `Originate` требует
`originate`. AMI проверяет наличие хотя бы одного разрешённого бита, поэтому
добавлять `reporting` и `call` к write ради этих операций не нужно.
`DeviceStateChange`, события канала и завершения вызова относятся к read `call`,
`DTMFBegin` — к `dtmf`. Нужен загруженный `res_manager_devicestate.so`.

`system` — широкий класс: он разрешает больше одного discovery action, включая
некоторые операции с модулями и чтение конфигурации. Это минимальные **классы** для
неизменённого upstream, не allowlist из четырёх безопасных команд. `read=all`,
`write=all`, `command` и `config` для описанного сценария не требуются. Для Redirect
или Setvar отдельно понадобится `write=call`; здесь они не используются.

Проверено по
[регистрации и проверке AMI actions](https://github.com/asterisk/asterisk/blob/22.11.0/main/manager.c),
[PJSIP action registration](https://github.com/asterisk/asterisk/blob/22.11.0/res/res_pjsip/pjsip_configuration.c),
[DeviceStateChange](https://github.com/asterisk/asterisk/blob/22.11.0/main/devicestate.c),
[DTMF и канальным событиям](https://github.com/asterisk/asterisk/blob/22.11.0/main/manager_channels.c).

## Точная схема HA action

Позвонить телефону 201; после ответа подключить к существующему echo 600:

```yaml
action: asterisk.send_action
data:
  action: Originate
  parameters:
    Channel: PJSIP/201
    Context: home
    Exten: "600"
    Priority: "1"
    CallerID: "Home Assistant <600>"
    Timeout: "20000"
    Async: "true"
```

Завершить конкретный существующий канал, полученный из текущего состояния PBX:

```yaml
action: asterisk.send_action
data:
  action: Hangup
  parameters:
    Channel: "PJSIP/201-00000001"
```

Второй пример иллюстрирует формат; канал динамический. Нельзя захардкодить suffix
или передавать пустой/устаревший Channel. HA script должен сначала убедиться, что
AMI подключён, строка Channel соответствует нужному endpoint и его текущее
состояние означает активный звонок. `send_action` не возвращает структурированный
результат в HA response variable: успешный вызов service не доказывает ответ
телефона. Контроль результата — события/датчики и фактический звонок.

Источники: [services.yaml](https://github.com/TECH7Fox/asterisk-hass-integration/blob/65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e/custom_components/asterisk/services.yaml),
[официальный Originate](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/AMI_Actions/Originate/).

## DTMF: автоматика через изменение датчика

Значение DTMF sensor — timestamp последнего события, цифра находится в атрибуте
`Digit`. Другие атрибуты: `Channel`, `ConnectedLineNum`, `ConnectedLineName`,
`Context`; у Sent также есть `CallerIDNum`/`CallerIDName`. Native HA event вроде
`asterisk_dtmf` upstream не публикует. Для повторных одинаковых цифр использовать
изменение timestamp, не только атрибута Digit.

Оба обработчика фильтруют AMI `DTMFBegin` по `ConnectedLineNum == extension`;
Sent/Received — направление на стороне Asterisk. Не считать автоматически, что
датчик `201 DTMF Received` означает клавиатуру именно 201. При первом испытании
смотреть оба конца звонка и Channel. Имена сущностей формируются HA и могут
получить suffix или быть переименованы. Все примеры `sensor.201_...` требуется
сопоставить с реальным entity registry, затем проверить цифры 1/1/# во время звонка.
Для RFC4733 Asterisk должен действительно получать аудио: `direct_media=no`.

## Assist по номеру 630

Проверены HA Core `2026.10.2` и его зависимость `voip-utils==0.5.0`.
Встроенный VoIP слушает **UDP 5060**; порт меняется через options `sip_port`.
Он не является сервером регистрации телефонов. Настраивается исходящий
PJSIP endpoint `ha-assist` с UDP transport, `disallow=all`, `allow=opus`,
`direct_media=no`, явным LAN-адресом HA и без outbound auth/registration.
Маршрут из `home`:

```asterisk
exten => 630,1,Dial(PJSIP/ha-assist/sip:630@HA_LAN_IP:5060,30)
 same => n,Hangup()
```

`qualify_frequency=0` для этого AOR: в проверенном `voip-utils` нет обработчика
OPTIONS, поэтому SIP qualify не является достоверным health check HA и может
запретить рабочий маршрут. Не задавать общий `from_user`: HA идентифицирует
устройство по SIP From URI. Сохранённый CallerID каждого внутреннего номера даёт
разные устройства HA, даже когда трафик приходит с одного IP Asterisk.

После первого вызова новое устройство появится в HA. Включить для него
**Allow calls**, выбрать Assist pipeline с рабочими STT и TTS и позвонить снова.
Без разрешения HA воспроизводит сообщение о ненастроенном устройстве. Без STT/TTS
pipeline — сообщение о проблеме конфигурации. Встроенные outgoing announcements
HA требуют отдельной настройки обратного SIP маршрута; один исходящий endpoint
к Assist не предоставляет их автоматически.

Источники:
[HA setup/UDP](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/voip/__init__.py),
[проверка pipeline](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/voip/voip.py),
[идентификация и Allow calls](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/voip/devices.py),
[SIP реализация](https://github.com/home-assistant-libs/voip-utils/blob/a6d93d0c50c8d005d030bd11ce99a57934b4e5b2/voip_utils/sip.py).

## Аудио и firewall

HA VoIP возвращает SDP `opus/48000/2`, `ptime:20`. Преобразование в mono 16 kHz
происходит уже внутри HA перед STT. Одного Asterisk Opus passthrough недостаточно:
7945G требует **G.711 ↔ Opus transcoding**. Поэтому Dockerfile собирает
`codec_opus_open_source.so` из
[Wazo commit 591b6cd](https://github.com/wazo-platform/wazo-codec-opus-open-source/tree/591b6cd9b75e2b9b6d815ef364a385fccee595a7)
с `libopus-dev` и сохраняет родной `res_format_attr_opus` из Asterisk. Это открытый
GPL-2.0 модуль; закрытый бинарный codec_opus не загружается. License, README и
соответствующий исходный архив находятся в контейнере:
`/usr/share/doc/asterisk-opus-open-source/`.

На стороне Asterisk сохраняется диапазон RTP `10000–10199`. На стороне HA
`voip-utils` выделяет динамический порт через `bind(..., 0)` и RTCP на следующем
порту; чётность RTP не гарантируется и настройки фиксированного диапазона в
интеграции нет. Правила HA firewall должны учитывать фактический ephemeral range
его ОС плюс соседний RTCP порт **только от IP PBX**. Настроить прямую маршрутизацию
до HA; простой Docker publish только 5060/UDP не публикует все динамические RTP
порты. На PBX разрешить SIP/RTP к HA и обратный поток; AMI открыть только HA→PBX.

Источник аудио и выбора портов:
[voip-utils 0.5.0 RTP server](https://github.com/home-assistant-libs/voip-utils/blob/a6d93d0c50c8d005d030bd11ce99a57934b4e5b2/voip_utils/voip.py).

## Выполненная проверка и оставшаяся приёмка

В изолированном ARM64 Docker-контейнере с Asterisk 22.11.0 и синтетической
конфигурацией проверено: открытый Opus module собирается, загружается как Running,
`core show translation recalc 1` завершается, PBX продолжает работать. Команды
`core show translation paths ulaw` и `... paths opus` показывают соответственно:

```text
(ulaw@8000)->(slin@8000)->(opus@48000)
(opus@48000)->(slin@8000)->(ulaw@8000)
```

Это проверка реального transcoder в контейнере, не испытание Cisco или живого HA.
Перед включением для семьи необходимы: установка upstream в фактический HA,
AMI discovery и reconnect, регистрация/отключение одного телефона, входящий и
исходящий звонок, hold/hangup, повторный DTMF, затем двусторонний разговор с Assist
через 630. Проверить STT русского языка, TTS, выполнение разрешённой безопасной
команды и освобождение всех каналов после завершения. По отдельности проверить
обычный звонок после рестарта Asterisk, HA и телефона.
