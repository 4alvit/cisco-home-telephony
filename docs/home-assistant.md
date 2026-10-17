# Home Assistant и Cisco 7945G

Полный HA-комплект: [ha-homelab/asterisk-telephony](https://github.com/ha-homelab/asterisk-telephony).
Он содержит установку, генератор HA package/dashboard, автоматизации, SVG и приёмку.
Этот документ отвечает за сторону Asterisk/k3s. [Исследование версий и API](home-assistant-research.md)
фиксирует ограничения существующей интеграции.

## Возможности и границы

Используется существующая **TECH7Fox Asterisk integration**, без копирования её
исходников в наш репозиторий. Она показывает PJSIP devices, состояние линии,
связанную линию и DTMF и предоставляет `asterisk.send_action`. Пакет HA даёт
вызов между внутренними аппаратами, вызов Assist и завершение конкретного канала.
Пример DTMF создаёт уведомление, а не открывает дверь и не управляет отоплением.

У upstream один глобальный сервис, поэтому поддерживается **одна АТС на HA**.
Сенсор registered отражает DeviceState, а не независимое доказательство SIP
регистрации; для диагностики смотрите также `pjsip show contacts`. DTMF приходит
через state/attributes сенсора, а не через отдельный HA event bus event.
Доступность конфигурации ещё не доказывает двусторонний звук физического телефона.

## 1. Включить AMI в локальной конфигурации

Добавьте в свой `.local/site.json` следующие поля, заменив адреса:

```json
"home_assistant": {
  "enabled": true,
  "ami_client_ipv4": "192.168.50.30",
  "allow_originate": true,
  "assist": {
    "enabled": true,
    "host_ipv4": "192.168.50.30",
    "sip_port": 5060
  }
}
```

Полный образец: [`site.ha.example.json`](../deploy/site.ha.example.json).
`ami_client_ipv4` — **адрес источника, который видит PBX после маршрутизации/NAT**,
не обязательно IP Pod или веб-интерфейса HA. `host_ipv4` — достижимый адрес
VoIP listener HA. Для удалённого HA используйте существующую доверенную VPN;
управление и Assist тогда идут через неё, обычные звонки между телефонами
остаются внутри LAN. Не публикуйте SIP, AMI или TFTP на WAN.

```bash
python3 scripts/provision.py --site .local/site.json --inventory .local/phones.json
bash scripts/deploy.sh
```

Генератор создаёт `.local/ha-ami-secret.json` с пользователем `homeassistant` и
случайным отдельным паролем. Файл имеет mode0600, исключён из Git и повторно
используется при генерации. Он не попадает в Cisco XML. Просмотрите его приватно
для настройки интеграции HA. `.local/secrets.json` по-прежнему содержит только
телефонные пароли. Резервируйте оба файла.

AMI слушает **IP выбранного PBX-узла:5038/TCP**, разрешает ровно один client /32,
HTTP manager отключён. Права: `read=call,dtmf`, `write=system,originate`.
`allow_originate:false` убирает только `originate`. **Это не read-only account**:
нужная для обнаружения PJSIP группа `system` также допускает Hangup и другие
административные AMI actions. Защищайте HA и эту учётную запись как управление АТС.
Права `command`, `config`, `all` не выдаются, CLI через AMI запрещён.

Интеграция использует обычный TCP AMI без TLS. Ограничьте firewall точным IP,
как в [шаблоне](../deploy/firewall.nft.example); ACL не шифрует соединение.

## 2. Подготовить Assist и сеть

В HA добавьте **Voice over IP**, проверьте SIP UDP port и настройте Assist pipeline
с работающими STT, conversation agent и TTS. Облачный LLM/STT не обязателен:
можно использовать локальные сервисы. Учтите их ресурсы и язык распознавания.

На HA должен быть достижим **UDP5060** или выбранный порт. Если HA запущен в k3s,
проверьте фактическую сеть Pod и маршрутизируемость объявленного в SDP адреса.
Простой HTTP Ingress HA не передаёт SIP/RTP. Обычно выбирают hostNetwork либо
отдельную маршрутизируемую сеть; не меняйте сеть существующего HA автоматически.

VoIP HA выбирает динамический UDP-порт RTP и соседний RTCP, фиксированного
настраиваемого RTP range у проверенной версии нет. Проверьте ephemeral range
ядра HA-host и разрешите его **только от IP PBX**. На стороне PBX разрешите от HA
UDP5060 и RTP10000–10199. SIP OPTIONS qualify здесь не используется: HA VoIP
не отвечает на него как обычная SIP-АТС. Входящих маршрутов из HA к телефонам нет;
управление вызовами идёт через AMI.

## 3. Собрать и развернуть обновлённый PBX

Следуйте [основному runbook](k3s-deployment.md): соберите оба образа, прогоните
smoke, выполните push в свой registry и обновите immutable digest в site.json.
Старый image без Opus не подходит для Assist.

```bash
bash scripts/build-images.sh YOUR_REGISTRY/cisco-home
bash scripts/smoke-containers.sh YOUR_REGISTRY/cisco-home
# После push, записи digest, проверки firewall и окна обслуживания:
bash scripts/deploy.sh --apply --firewall-reviewed
```

Opus построен из GPL-2.0 исходников Wazo, закреплённых commit+SHA256. Исходный
архив, LICENSE и README хранятся в образе под
`/usr/share/doc/asterisk-opus-open-source/`. Это transcoding G.711↔Opus, не только
объявление кодека в SDP. Readiness с включённым Assist требует работающего модуля.

Генерация добавляет **630** в Cisco dialplan. Обновите TFTP-профили и дайте
телефону загрузить новый dialplan в отдельное окно по исходной инструкции.
Переустанавливать firmware ради изменения номера не требуется.

## 4. Подключить интеграцию и проверить

Установите проверенную upstream версию через инструкции companion-репозитория,
перезапустите HA в согласованное окно, затем добавьте Asterisk через UI:
IP PBX, TCP5038, `homeassistant`, пароль из локального файла.
После обнаружения устройств впишите **реальные entity IDs** в локальный inventory
генератора HA package/dashboard. Не ожидайте, что автоматически созданные IDs
точно совпадут с примерами.

1. Проверьте `pjsip show contacts`, состояние устройств в HA и отсутствие ошибок AMI.
2. Выполните внутренний вызов двух аппаратов и проверьте изменение состояния.
3. Выполните вызов через HA script; завершите только его фактический Channel.
4. Проверьте DTMF на тестовом звонке и безопасное уведомление HA.
5. Наберите630. При первом обнаружении устройства в HA включите **Allow calls**
   и выберите pipeline; затем повторите вызов. SIP From сохраняет номер телефона,
   поэтому устройства разных аппаратов не должны сливаться в один identity.
6. Проверьте STT, ответ TTS, двусторонний звук и завершение; повторите после
   перезапуска PBX/HA и cold boot телефона. Начинайте с одного аппарата.

## Откат

Удалите/отключите HA package, затем задайте `home_assistant.enabled:false`,
повторите генерацию и стандартный deployment. Это закрывает AMI в Asterisk и
удаляет маршрут630 из новых конфигов. Уберите HA firewall allowances и обновите
телефонный dialplan при следующем TFTP-окне. Для полного восстановления используйте
старый immutable image/config Secret по [runbook отката](operations.md).
Не удаляйте существующие HA `.storage`, HACS, секреты, записи устройств или базы.
