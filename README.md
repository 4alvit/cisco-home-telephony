# Cisco 7945G: домашняя телефония на Asterisk и k3s

Инструкции для купленной на eBay партии **10 × Cisco 7945G**.
Модель подтверждена письмом о доставке от **1 декабря 2022 года**.
[Подтверждение покупки](docs/purchase-evidence.md).

**Эти телефоны уже поддерживают PoE IEEE 802.3af.** Переделывать телефон не нужно.
Подойдёт PoE-коммутатор или активный совместимый инжектор; питание подаётся в порт
**SW**, а не PC. Для работы с Asterisk используется родная **SIP firmware для
7945/7965 (SIP45)**. Переход на MPP здесь не нужен.

![Схема домашней телефонии](docs/diagrams/architecture.svg)

## Что готово

- Генератор индивидуальных `SEP<MAC>.cnf.xml`, dialplan и Asterisk PJSIP.
- Инвентаризация на десять телефонов с обязательной проверкой модели и MAC.
- Раздельные контейнеры Asterisk и временного TFTP, декларативный deployment k3s.
- Сухая проверка по умолчанию, явный выбор kube context и узла, образы по digest.
- Проверка целостности локального firmware, сценарии приёмки, резервирования и отката.
- [SVG-схемы](docs/diagrams/) и [цветные слайды](docs/slides/).
- [Home Assistant](docs/home-assistant.md): состояния телефонов, DTMF, управление вызовами и Assist по номеру **630**.

Конфигурации и автоматические проверки подготовлены; **домашний кластер и телефоны
в этой задаче не изменялись**. Прошивку и регистрацию физических 7945G ещё нужно
проверить на одном аппарате. Официальные Cisco firmware не включены: потребуется
собственная законно полученная копия и подходящий путь обновления.

## Порядок работы

1. [Проверить питание и PoE](docs/poe.md).
2. [Снять инвентаризацию и выбрать путь SCCP → SIP](docs/sip-conversion.md).
3. [Сгенерировать приватные конфиги](docs/provisioning.md).
4. [Собрать образы и развернуть Asterisk/TFTP в k3s](docs/k3s-deployment.md).
5. [Проверить звонки, закрыть TFTP, сохранить резервную копию](docs/operations.md).

До физической инвентаризации можно отдельно [подготовить PBX и AMI](docs/pbx-bootstrap.md)
через `provision.py --pbx-only`: без выдуманных телефонов, Cisco XML и TFTP.

**Перед reset/flash:** для 7945G аппаратной версии **13.0 и новее** нельзя
откатываться ниже **9.3(1)SR3**. Популярная инструкция «всем сначала 8.5(2)» опасна
для этих ревизий. Текущую прошивку и hardware revision смотрят на каждом телефоне.

## Быстрый старт локальной подготовки

```bash
git clone https://github.com/4alvit/cisco-home-telephony.git
cd cisco-home-telephony
umask 077
mkdir -p .local/firmware
cp deploy/site.example.json .local/site.json
cp examples/phones.example.json .local/phones.json
cp examples/firmware-manifest.example.json .local/firmware-manifest.json
```

Заполните IP/NTP, проверенные MAC, аппаратные ревизии и firmware load.
Примеры намеренно не проходят валидацию до заполнения. Не меняйте `verified`
на `true`, пока данные не сверены с физическим аппаратом.

```bash
python3 scripts/provision.py --site .local/site.json --inventory .local/phones.json
python3 scripts/verify_firmware.py --directory .local/firmware --manifest .local/firmware-manifest.json
python3 -m unittest discover -s tests -v
# После сборки образов и внесения их digest в site.json:
bash scripts/deploy.sh --with-tftp
```

Последняя команда **не обращается к кластеру**. Реальный apply, правила firewall,
DHCP option 150, staging firmware на узле и порядок приёмки описаны в
[полном runbook](docs/k3s-deployment.md). Не запускайте apply до его прочтения.

## Номера и сеть

Номера примера: **201–210**. **600** — эхо, **601** — тестовый тон,
**620** — одновременный вызов всей группы. При включённой HA-интеграции
**630** — голосовой помощник Home Assistant Assist. Исходящих PSTN, trunk, voicemail и
экстренных вызовов нет. Для звонков в экстренные службы используйте действующую
внешнюю телефонную связь.

Профиль по умолчанию: **SIP TCP 5060**, G.711, RTP через Asterisk.
Сервер также слушает UDP 5060 для явного UDP-профиля. RTP Asterisk:
UDP **10000–10199**, RTP телефона: **16384–16482**. Временный TFTP:
UDP **69 + 20000–20031**, до одного часа на окно настройки. Доверенный LAN/VLAN,
без публикации в интернет. SIP/TFTP здесь не шифруются.

В публичных примерах используются условные значения: `voice-node`,
`192.168.50.10`, контекст `home-k3s`. Замените их своими в `.local/site.json`.
Проверка узла и отсутствия конфликтов портов обязательна. Облачные узлы
не участвуют в медиапути.

## Приватные данные

`.local/` игнорируется Git и содержит пароли, MAC и готовые Secret.
Повторный запуск генератора сохраняет пароли из `.local/secrets.json`.
Потеря этого файла означает необходимость восстановить backup либо заново
согласованно настроить телефоны и PBX. Не коммитьте vendor firmware, kubeconfig,
capture звонков или сырые письма. Репозиторий публичный; реальные параметры сети и личные данные остаются локально.

[Проверенные технические источники](docs/cisco-research.md) объясняют ограничения
Cisco и границы поддержки Asterisk.

## Бесплатный CI

[GitHub Actions](https://github.com/4alvit/cisco-home-telephony/actions) проверяет
Python, shell, сборку Asterisk/TFTP и запуск контейнеров на стандартном
`ubuntu-24.04`. Для публичного репозитория эти runner minutes бесплатны.
Workflow не использует платные larger runners, внешние платные сервисы или
увеличенные cache quotas. После успешного smoke только push в `main` сохраняет
проверенные amd64-образы: суммарно меньше 400 MiB, срок хранения один день.
После подтверждённого скачивания и импорта удаляйте именно этот deployment
artifact. Хранение artifacts имеет отдельную квоту и не относится к бесплатным
runner minutes: [правила оплаты Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

## Home Assistant

[Пошаговое подключение](docs/home-assistant.md) включает AMI с отдельной учётной
записью, ACL на один IP и внутренний маршрут к Assist. HA-пакеты, панель, установка
проверенной версии существующей интеграции и SVG-схемы находятся в публичном
[ha-homelab/asterisk-telephony](https://github.com/ha-homelab/asterisk-telephony).
Репозиторий создан через существующий [Terraform организации](https://github.com/4alvit/terraform-github-ha-homelab/blob/main/asterisk-telephony.tf).

Для Assist образ содержит собранный из исходников Opus transcoder: Cisco использует
G.711, а встроенный VoIP Home Assistant — Opus. AMI выключен по умолчанию;
`deploy/site.ha.example.json` показывает явное включение.
