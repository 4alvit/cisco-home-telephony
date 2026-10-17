# Проверенные сведения о партии Cisco

Проверено 16 октября 2026 года. В Gmail найдено сообщение о партии **10 × Cisco 7945G**, с трубками и подставками, доставка 1 декабря 2022 года. Это устанавливает модель заказа, но не текущую исправность, MAC, аппаратные ревизии и прошивки экземпляров. Перед массовой настройкой сверить этикетки.

- **7945G имеет цветной экран, две клавиши линий, Gigabit Ethernet, поддержку SCCP и SIP.** KEM для этой модели не предусмотрен. [Cisco, сравнение телефонов](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cvd/G2srmdbrnt/G2srmdbrnt_Book/FeatServ.html).
- **PoE встроен: IEEE 802.3af, Class 3.** Переделывать аппарат не нужно. [Cisco PoE FAQ](https://www.cisco.com/c/en/us/support/docs/voice-unified-communications/unified-ip-phone-7900-series/97869-poe-requirement-faq.html). Схема подключения и бюджет: [poe.md](poe.md).
- Ветка прошивок — **`7945_7965`**, SIP и SCCP поставляются отдельно. Это Enterprise SIP, а не лицензируемая MPP-конверсия семейства 7800/8800.
- **9.4(2)SR3 — не последний обнаруженный выпуск.** Официальный device pack сентября 2025 года содержит `7945_7965-sip.9-4-2SR4-3`, версию `9.4(2SR4.3)`. Это ориентир для получения пакета, не подтверждение уже выполненной прошивки. [Cisco Device Package 14.0(1.15072-1)](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cucm/device_packs/1401-devpack-for-sep-2025.html).
- Cisco допускает ZIP-поставку SIP firmware с ручным размещением на TFTP без CUCM. Подписанные компоненты должны оставаться комплектом; одного `.loads` недостаточно. [Cisco 7945/7965 SIP README](https://www.cisco.com/web/software/282074288/128566/cmterm-7945_7965-sip.9-4-2SR2-2-readme.html).
- **Путь обновления зависит от hardware и App Load.** При исходной версии ниже 8.3(3) нужна промежуточная версия. Для hardware 13.0+ в release notes 9.3(1)SR4 минимум указан как 9.3(1)SR3. Поэтому нельзя сначала прошивать все аппараты в 8.5(2). [Cisco release notes: Hardware Updates / Installation](https://www.cisco.com/en/US/docs/voice_ip_comm/cuipph/7900_series/firmware/9_3_1SR4/english/release_notes/P790_BK_RF3660D6_00_rn-9_3_1sr4-7900-series_chapter_00.pdf).
- Поддержка 7945G закончилась **30 июня 2023 года**. Использовать выделенную voice-сеть и локальные сервисы. [Cisco 7900 support page](https://www.cisco.com/c/en/us/support/collaboration-endpoints/unified-ip-phone-7900-series/series.html).

Не установлены App Load/VID отдельных телефонов, доступность разрешённого firmware-архива и параметры домашнего оборудования. Реальные звонки ещё не испытаны. Скрипты создают конфигурацию; это не доказательство физического развёртывания.

Полный порядок: [sip-conversion.md](sip-conversion.md). Cisco подтверждает свойства оборудования, но не гарантирует приведённую интеграцию с Asterisk. Для XML дополнительно использована [первичная документация автора USECALLMANAGER](https://usecallmanager.nz/sepmac-cnf-xml.html). Сначала нужен пилот на одном аппарате.
