# Эксплуатация и восстановление

## Обычная проверка

```bash
kubectl --context home-k3s -n telephony get pods -o wide
kubectl --context home-k3s -n telephony logs deploy/asterisk --since=15m
kubectl --context home-k3s -n telephony exec deploy/asterisk -- asterisk -rx 'pjsip show contacts'
kubectl --context home-k3s -n telephony exec deploy/asterisk -- asterisk -rx 'core show channels count'
```

Телефоны должны быть зарегистрированы на IP домашнего узла; звуковые пакеты идут
между ним и телефонами. Проверяйте свободное место, память, рестарты и потери
пакетов на проводном интерфейсе. Не переносите Pod автоматически на другой узел:
телефоны и option150 ссылаются на фиксированный адрес.

Проверка готовности подтверждает наличие обоих SIP transport в Asterisk, а не качество звонка. После
каждого изменения нужны реальный двусторонний звонок и echo600 с Cisco. Для
постоянной проверки звука можно позднее добавить SIPp сценарий со своим тестовым
endpoint; мониторинг не должен сохранять пароли и записи звонков в общий лог.

## Что сохраняется

Эта базовая АТС **stateless**: `AstDB`, spool и file logs лежат в `emptyDir`,
исчезающем при пересоздании Pod. Voicemail, записи, постоянный CDR и хранимое в
AstDB состояние не включены. Регистрации восстанавливаются телефонами после
перезапуска. Добавление voicemail/recording требует отдельного PVC, retention,
шифрования и проверки восстановления; нельзя считать текущий backup бэкапом
несуществующего persistent volume.

Источник конфигурации — Git с кодом плюс **отдельно** `.local/`: site, проверенный
inventory, секреты, XML, Asterisk конфиги, manifest и лицензированная firmware.
Kubernetes Secrets не заменяют этот backup. Версии образов закреплены digest;
сохраните образы в надёжном registry или экспортируйте `docker save` отдельно,
чтобы garbage collection registry не лишил возможности отката.

## Шифрованный backup и восстановление

Нужен установленный `age` и ваш заранее созданный публичный recipient.

```bash
# AGE_RECIPIENT — ваш age1... публичный recipient, не приватный ключ.
bash scripts/backup.sh "$AGE_RECIPIENT" "$HOME/backups/cisco-home-2026-10-16.tar.gz.age"
# Проверка расшифрования/архива без вывода паролей (имена файлов приватны):
age -d -i "$AGE_IDENTITY_FILE" "$HOME/backups/cisco-home-2026-10-16.tar.gz.age" | tar -tzf -
```

Каталог назначения должен существовать. Храните хотя бы одну шифрованную копию
вне выбранного k3s узла; приватный age key — отдельно от backup. Скрипт не
перезаписывает существующий файл. Восстановление делайте в новый приватный
каталог, чтобы не затереть текущую рабочую конфигурацию:

```bash
umask 077
mkdir -p "$HOME/cisco-restore-check"
age -d -i "$AGE_IDENTITY_FILE" "$HOME/backups/cisco-home-2026-10-16.tar.gz.age" | \
  tar -xzf - -C "$HOME/cisco-restore-check"
python3 scripts/preflight.py --site "$HOME/cisco-restore-check/.local/site.json" --with-tftp
```

После проверки сравните site/node IP, firmware hashes, inventory и digest образов,
выберите окно, перенесите восстановленную `.local` в checkout и примените обычный
процесс. Восстановление на новом IP требует изменений DHCP/TFTP/phone config и
не является прозрачным failover.

## Обновления и откат

Перед изменением убедитесь `core show channels count`, что нет активных звонков,
сделайте encrypted backup и сохраните Git SHA. Новый Asterisk сначала проходит
сборку, smoke, затем один реальный телефон. Не обновляйте firmware десяти
телефонов одновременно с версией Asterisk.

Изменение любого `.conf` создаёт новый immutable Secret с hash в имени. Pod
получает новое имя Secret и пересоздаётся через `Recreate`. Это **прерывает звонки**:
обновление выполняется в окно. Старые Secrets намеренно не удаляются автоматически.

```bash
kubectl --context home-k3s -n telephony rollout history deployment/asterisk
kubectl --context home-k3s -n telephony rollout undo deployment/asterisk
kubectl --context home-k3s -n telephony rollout status deployment/asterisk --timeout=180s
```

Откат ReplicaSet возвращает прежний digest и ссылку на прежний Secret. Он работает,
пока старый Secret существует и image доступен. Не используйте эту команду для
отката firmware: bootloader/подписи/допустимый downgrade зависят от текущего load.
После отката Kubernetes восстановите соответствующую локальную конфигурацию из
backup/Git; иначе следующий apply вновь внесёт неудачное изменение.

Если менялись SIP credentials, откат Asterisk отдельно от телефонного XML может
рассинхронизировать пароли. Восстанавливайте совпадающий набор Secrets/XML и
обновляйте телефоны в TFTP окно. Новые пароли генерируйте локально и не присылайте
их в тикеты. Старые immutable Secret удаляйте только после проверенного backup и
истечения принятого окна rollback; не удаляйте ссылки действующих ReplicaSet.

## Неисправности

**Pod Pending:** проверьте точное имя узла, Ready/cordon/taints, requests и свободную
память. Жёсткая привязка — ожидаемое поведение. Не удаляйте чужие taints и не
переносите Pod на OCI без изменения архитектуры.

**ImagePullBackOff:** digest должен существовать, образ должен иметь архитектуру
узла, registry должен быть доступен именно от containerd этого узла. Проверяйте
`kubectl describe pod` и локальный k3s journal. Секреты registry в логи не выводите.
`localhost:5000` на ноутбуке и на узле — разные адреса; используйте явный registry.

**CrashLoopBackOff / bind failed:** проверьте конфиг и права, что IP действительно
есть на узле, а 5060 свободен. Второй Pod Asterisk или другой SIP daemon займёт
тот же порт. При read-only rootfs писать разрешено только в смонтированные каталоги.
Не обходите ошибку запуском Asterisk privileged/root.

**Телефон не запрашивает firmware:** проверьте физическую модель/MAC, DHCP, VLAN,
TFTP IP option150/Alternate TFTP, имя `SEP<MAC>.cnf.xml`, состояние TFTP Job и
наличие точного `.loads` плюс всех `.sbn`. Штатный TFTP использует UDP69 и отдельный
диапазон UDP20000–20031. Если RRQ виден, но файл не приходит, часто закрыт обратный
UDP, выбран неправильный VLAN или TTL Job истёк. Файл должен быть read-only 0644.

**TFTP Job Failed/DeadlineExceeded:** через час это ожидаемое завершение окна.
Проверьте отсутствие активной прошивки, удалите старый Job, заново откройте
firewall и повторите `deploy.sh --apply --firewall-reviewed --with-tftp`.
Не запускайте два provisioning daemon на одном IP:69.

**Регистрация отсутствует:** проверьте SIP firmware, TCP/UDP выбор в XML и
Asterisk, совпадение authName/extension/password, IP сервера, permit CIDR,
время/NTP и отсутствие чужого ITL/CTL согласно инструкции конверсии. Обычно
первый SIP 401 — нормальный digest challenge; повторяющиеся 401/403 без успешного
REGISTER требуют проверки credentials/config. Настройка CUCM и MPP здесь не
применяется к CP-7945G.

**Звонок есть, звука нет/односторонний:** SIP5060 и RTP — отдельные потоки. Проверьте
UDP10000–10199 до Asterisk и обратный трафик к RTP портам телефона; в SDP должны
быть реальные LAN IP, не PodCIDR/OCI/WireGuard адрес. Кодеки обоих концов —
G.711 µ-law/A-law. В этой схеме `direct_media=no`, поэтому звук проходит через
Asterisk и его host firewall. Отключение SIP ALG на пограничном роутере уместно,
если он реально находится в SIP пути; не меняйте чужой firewall наугад.

**Контакты пропадают после restart:** устройства должны перерегистрироваться.
Проверьте cold boot после закрытия TFTP: конкретный load может снова требовать
config с TFTP. На время такого boot открывается новое узкое окно.

Диагностический packet capture разрешайте лишь на короткое окно: SIP и TFTP могут
содержать учётные данные, телефонные номера и звук. Храните pcap приватно и удаляйте
по окончании расследования; не коммитьте `.pcap`.

## Остановка и демонтаж

```bash
# Завершите текущие flash операции и звонки заранее.
kubectl --context home-k3s -n telephony delete job cisco-tftp --ignore-not-found
kubectl --context home-k3s -n telephony scale deployment/asterisk --replicas=0
```

Затем уберите временные firewall rules и option150 только из выделенного phone
scope. Firmware на узле не удаляется скриптами автоматически. Полный демонтаж
(namespace/Secrets/images) делайте после проверенного backup и списка объектов
именно проекта; `delete namespace` уничтожает все объекты этого namespace.

## Пределы проверок этой поставки

Python unit/integration tests и shell syntax checks выполняются локально. В среде
подготовки Docker daemon отсутствовал; runtime сборка проверяется workflow
`Validate and build` в GitHub Actions. Успешный workflow не заменяет проверку
аппаратного Cisco boot, лицензированной firmware, PoE, реальных DHCP/firewall и
звонка в доме. Кластер и телефоны в рамках подготовки репозитория не менялись.
