# Проверка серверной установки — 17 октября 2026

Серверная часть установлена в существующем домашнем k3s. Этот протокол
не является приёмкой физических Cisco: ни один аппарат пока не прошёл
инвентаризацию, перепрошивку, регистрацию или проверку звука.

## Выполнено на реальной системе

- Выделенный namespace, один Asterisk **22.11.0** на домашнем Linux AMD64 узле.
  Контейнер работает как UID10001, без capabilities, с read-only rootfs.
  Deployment использует проверенный immutable image digest.
- SIP TCP/UDP, endpoint `ha-assist`, маршрут **630** и работающий Opus проверены
  в живом контейнере. Подтверждены пути G.711 μ-law → Opus и Opus → G.711.
- Readiness проверяет оба SIP transport, AMI LISTEN на заданном IPv4:5038,
  модуль Opus, endpoint и маршрут Assist. Успешный rollout не заменяет звонок.
- Установлены постоянные ограничения firewall на PBX и HA узлах, включая IPv6.
  AMI разрешён только с фактического адреса HA; доступ с рабочей станции отклонён.
  Чужие правила firewall сохранены. TFTP закрыт, TFTP Job не запущен.
- В существующем **Home Assistant 2026.10.0** установлена закреплённая
  TECH7Fox integration **1.0.5**. Asterisk и встроенный VoIP имеют состояние
  `loaded`; VoIP слушает UDP5060. `AMI Connected` включён.
- Из самого HA-container успешно выполнены AMI Login, CoreSettings,
  PJSIPShowEndpoints и Logoff. Версия совпала, endpoint найден, CLI Command
  получил ожидаемый отказ. Вызов HA service `asterisk.send_action` с Ping
  принят; ответ сервиса сам по себе не считается проверкой звонка.
- В HA создана отдельная административная панель **«Телефония»** с реальным
  AMI sensor, инструкцией первого аппарата и ссылками на документацию.
  Конфигурация панели проверена обратным чтением через API.
- Перед установкой сделана локальная резервная копия HA configuration и
  `.storage`. После внутреннего перезапуска HA вернулся в `RUNNING`, safe mode
  выключен; все **120 автоматизаций и 94 скрипта** сохранились.

Образы собраны на стандартном GitHub `ubuntu-24.04` runner и прошли
[native AMD64 build и изолированный smoke](https://github.com/4alvit/cisco-home-telephony/actions/runs/39029793014)
из commit `f5cb1546ed93c53d4792817d0bb1ba0f3a929821`. Checksums архива,
архитектура и registry manifest/config digest сверены перед deployment.
Временный GitHub artifact удалён после доставки. Адрес registry, полные
deployment receipts, настройки firewall, entity maps и секреты сохранены только
локально. Последующие изменения readiness не меняли Dockerfile этих образов.

## Осталось выполнить с настоящими телефонами

1. Найти и включить один **CP-7945G** через порт SW и PoE 802.3af.
2. Записать MAC, аппаратную ревизию, Active/App Load, Boot Load и сетевые
   параметры. До этого не выбирать downgrade/reset/flash.
3. Получить подходящую лицензированную SIP45 firmware и проверить путь
   обновления по [инструкции](sip-conversion.md). Firmware в Git отсутствует.
4. Дополнить приватный bootstrap inventory, сгенерировать настоящие профили,
   открыть ограниченное окно TFTP и проверить первый аппарат.
5. Проверить регистрацию, echo600, звонок между двумя аппаратами, DTMF и
   двусторонний звук. Для Assist после первого вызова разрешить звонки для
   реального VoIP device и выбрать рабочий STT/TTS pipeline.
6. Сопоставить настоящие HA entity ID и установить панель управления звонками
   из [companion repository](https://github.com/ha-homelab/asterisk-telephony).

Сейчас нет подтверждения физического питания/подключения партии. Наличие
служебного `ha-assist` в AMI не означает наличие или регистрацию Cisco.
Готовый Opus transcoder и открытый listener не доказывают качество сети,
работу выбранного Assist pipeline либо звук в телефонной трубке.
