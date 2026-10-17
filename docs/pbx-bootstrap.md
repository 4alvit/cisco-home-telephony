# Подготовка PBX до подключения телефонов

Если физические MAC-адреса и прошивки ещё не проверены, можно подготовить
сервер без фиктивного inventory. Явный режим `--pbx-only` создаёт только
Asterisk-конфигурацию, а при `home_assistant.enabled: true` — AMI и его отдельный
локальный пароль. Никаких телефонных endpoint/auth, Cisco XML, `secrets.json`
для SIP-телефонов и каталога TFTP он не создаёт. NTP нужен телефонам, поэтому
в этом режиме `ntp_ipv4` не требуется.

Используйте отдельный приватный каталог, например `.local/pbx-bootstrap`.
Генератор откажется работать поверх `tftp` или `secrets.json`, оставшихся от
телефонного provisioning: он не удаляет их и не выдаёт старые профили за новую
конфигурацию. Повторный запуск в том же bootstrap-каталоге сохраняет пароль AMI.

1. Создайте в нём `site.json` с реальными параметрами кластера и сети по
   [инструкции deployment](k3s-deployment.md). `asterisk_image` должен содержать
   проверенный digest образа для архитектуры выбранного узла. TFTP-образ и
   прошивка для запуска PBX не нужны.
2. Если нужна интеграция HA, задайте блок `home_assistant` по
   [инструкции AMI](home-assistant.md). `ami_client_ipv4` — наблюдаемый на PBX
   адрес клиента, а не предположение по Pod CIDR. Начните с
   `allow_originate: false` и `assist: {"enabled": false}`; включайте команды
   звонков и Assist после отдельной проверки необходимых условий.
3. Сгенерируйте и проверьте конфигурацию:

```bash
umask 077
python3 scripts/provision.py --pbx-only \
  --site .local/pbx-bootstrap/site.json --out .local/pbx-bootstrap
python3 scripts/preflight.py --site .local/pbx-bootstrap/site.json --cluster
./scripts/deploy.sh --site .local/pbx-bootstrap/site.json
# Изучите private rendered/core.yaml, firewall и выбранный context/node.
# Применяйте только после этой проверки:
./scripts/deploy.sh --site .local/pbx-bootstrap/site.json --apply --firewall-reviewed
```

Без `--apply` deployment лишь готовит файлы. **Не добавляйте `--with-tftp`**:
bootstrap не содержит ни телефонных профилей, ни проверенной firmware.
Существующий host firewall сохраняется; AMI TCP5038 разрешается только с
адреса HA. Ни AMI, ни SIP/RTP не публикуются в интернет.

В bootstrap доступны внутренние приложения **600 — Echo** и **601 —
Milliwatt**. Группы 620 и телефонных номеров 2xx нет. При явном включении
Assist добавляется только маршрут 630 к проверенному VoIP listener HA.
Без зарегистрированного телефона приложения можно проверить контролируемым
локальным/AMI-вызовом; такая проверка не доказывает звук на физическом Cisco.
После запуска проверяйте SIP transports и AMI login, а не наличие телефонных
entities: их до подключения аппаратов не ожидается.

При включённом Assist upstream также обнаруживает служебный endpoint
`ha-assist`. Его производный `Registered` может быть выключен, а `State` —
`Unavailable`: этот endpoint набирается по явному SIP URI и не регистрируется
на HA. Это не физический Cisco и не проверка доступности Assist. Серверный
`AMI Connected` и отдельный [AMI-пробник](https://github.com/ha-homelab/asterisk-telephony/blob/main/docs/ami-verification.md)
проверяют управление АТС; работу аудио подтверждает отдельный звонок.

## Добавление проверенных аппаратов

После физической проверки создайте настоящий `phones.json`, добавьте NTP и
выполните обычный generator в **том же** каталоге, сохранив AMI-пароль:

```bash
python3 scripts/provision.py --site .local/pbx-bootstrap/site.json \
  --inventory .local/pbx-bootstrap/phones.json --out .local/pbx-bootstrap
```

Обычный режим по-прежнему требует 1–50 физически проверенных CP-7945G. Он
создаёт телефонные auth/XML и группу 620; эхо и тестовый тон сохраняют номера
600 и 601. Далее выполните
отдельную проверку firmware, preflight и deployment по основному runbook.
Файлы из `.local`, пароли, MAC и private rendered manifests не коммитятся.
