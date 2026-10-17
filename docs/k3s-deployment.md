# Cisco 7945G → Asterisk в домашнем k3s

Эта инструкция разворачивает внутреннюю АТС для десяти Cisco CP-7945G. Телефоны
должны быть переведены на подходящую **SIP firmware** по отдельной инструкции.
Внешний SIP trunk, PSTN/экстренные вызовы, voicemail и публичный доступ здесь не
настраиваются. Два телефона, echo `600`, тон `601` и группа `620` позволяют
проверить весь локальный маршрут.

## 1. Выбрать домашний узел

Публичные примеры используют условную сеть: узел `voice-node`, IP
`192.168.50.10`, LAN `192.168.50.0/24`, контекст `home-k3s` и registry
`192.168.50.20:5050`. Это **не реальные параметры и не результат проверки
живого кластера**. Сохраните собственные значения в `.local/site.json`.

Выберите проводной Linux worker внутри дома: голос должен идти по LAN, а не
через облачный control plane или VPN. Слово «фиксированный» означает узел для
этого Deployment, а не незанятый физический компьютер. Проверьте ресурсы, порты
и архитектуру CPU. Не переустанавливайте существующий k3s ради этой АТС.

Пример registry использует HTTP в доверенной домашней сети. Для него отдельно
нужны настройка **Docker builder для push** и настройка **k3s/containerd для pull**.
[Prerequisite для HTTP registry](../deploy/README.md#plain-http-registry-prerequisite)
описывает точечную настройку; существующий mirror k3s не настраивает Docker.
Предпочтителен registry с TLS. Image digest обязателен в любом случае.

У Asterisk один Pod, `hostNetwork: true`, фиксация на `metadata.name` узла,
`replicas: 1`, стратегия `Recreate`. SIP и RTP используют IP узла. Нет Kubernetes
Service/Ingress/NodePort/LoadBalancer, DNS туннеля, порт-форвардинга на роутере или
DHCP workload. Не масштабируйте до двух экземпляров: они конкурируют за одни порты.

## 2. Подготовить локальные параметры и firmware

Нужны Python 3.9+, kubectl с существующим доступом, Docker/BuildKit на подходящем
builder, SSH к выбранному узлу и лицензированная firmware Cisco. Архитектура image должна соответствовать узлу. Скрипт по умолчанию собирает
`linux/amd64`; для ARM узла задайте `BUILD_PLATFORM=linux/arm64`. Для amd64
используйте native amd64 builder или настроенную эмуляцию BuildKit.

```bash
umask 077
mkdir -p .local/firmware
cp deploy/site.example.json .local/site.json
# Отредактируйте .local/site.json: подтвердите node/IP/CIDR/context,
# укажите ntp_ipv4 существующего доступного NTP server (узел не обязан им быть).
# Используйте пример inventory из README и физически проверенные MAC.
python3 scripts/provision.py --site .local/site.json --inventory .local/phones.json
python3 scripts/verify_firmware.py --directory .local/firmware --manifest .local/firmware-manifest.json
```

Последняя команда должна пройти до копирования firmware на узел. Не скачивайте
случайные «прошивки» из форумов. Не помещайте vendor firmware, MAC/inventory,
пароли, kubeconfig, сгенерированные XML или манифесты Secret в Git.

## 3. Собрать, проверить и опубликовать образы

Для AMD64-узла при локальном ARM-компьютере доступна
[нативная сборка и одноразовый экспорт через GitHub Actions](ci-images.md).
Она использует те же Dockerfile и smoke; обычные push/PR artifacts не сохраняют.

Закреплена версия Asterisk **22.11.0 LTS**, объявленная официальным сайтом;
SHA-256 исходного архива зафиксирован в Dockerfile. Это контроль неизменности
проверенного HTTPS-архива, а не заявление о проверке отдельной GPG-подписи.
Базовый Debian `bookworm-slim` разрешается в digest перед сборкой. APT зависимости
берутся из актуального Debian репозитория, поэтому сборка не является побитно
воспроизводимой. Финальные deployment image references обязательно содержат digest.

```bash
# На amd64 builder для voice-node. Эта команда не выполняет push.
bash scripts/build-images.sh 192.168.50.20:5050/cisco-home
# Публикация в своём доступном registry:
docker push 192.168.50.20:5050/cisco-home/asterisk:22.11.0
docker push 192.168.50.20:5050/cisco-home/tftp:bookworm
docker image inspect --format '{{join .RepoDigests "\n"}}' \
  192.168.50.20:5050/cisco-home/asterisk:22.11.0
docker image inspect --format '{{join .RepoDigests "\n"}}' \
  192.168.50.20:5050/cisco-home/tftp:bookworm
```

Для HTTP registry настройка `registries.yaml` на k3s node не настраивает Docker
builder. У последнего отдельно требуется точечный `insecure-registries` для
вашего `HOST:PORT`: [порядок и ограничения](../deploy/README.md#plain-http-registry-prerequisite).
Не перезаписывайте существующий daemon.json и не перезапускайте чужой builder без окна.

Впишите соответствующие `repository@sha256:…` в `asterisk_image` и `tftp_image`
файла `.local/site.json`. Если registry требует авторизации, создайте pull Secret
в namespace `telephony` из отдельного Docker config файла и укажите **имя** в
`image_pull_secrets`. Не добавляйте PAT/password в site.json или shell history.

Проверка образов с настоящим генератором конфигурации, но синтетическим MAC,
случайным временным паролем и loopback IP, без доступа контейнера к LAN:

```bash
bash scripts/smoke-containers.sh 192.168.50.20:5050/cisco-home
```

Smoke проверяет загрузку `chan_pjsip`, endpoint 201, UDP/TCP транспорты и dialplan
600/601/620; затем выполняет реальный TFTP RRQ и проверяет init script как UID10001
на root-owned томе с доступом через fsGroup. Asterisk работает без сети, а TFTP —
в одноразовой Docker internal network без публикации портов. Для совместимости
с remote Docker context синтетические конфиги передаются в одноразовых fixture images,
которые удаляются вместе с тестовыми контейнерами и сетью. Точно эти проверки запускаются в GitHub Actions после сборки двух
образов. Реальную регистрацию Cisco и двусторонний звук проверяют отдельно в LAN.

## 4. Проверить узел, firewall и DHCP

```bash
kubectl --context home-k3s get nodes -o wide
python3 scripts/preflight.py --site .local/site.json --cluster --with-tftp
# На выбранном узле (read-only):
ssh voice-node 'ip -br addr; sudo ss -lntup; df -h /var/lib; free -h'
ssh voice-node 'sudo nft list ruleset'
```

Заранее проверьте отсутствие других владельцев TCP/UDP `5060`, UDP `69`, UDP
`10000–10199` и UDP `20000–20031`. `ss` не доказывает отсутствие динамического
использования диапазона — сопоставьте с другими hostNetwork workload и конфигами.
Оставьте минимум 512 MiB RAM и 1 CPU лимита для Asterisk; короткие LAN звонки
обычно требуют меньше, но нагрузку и звук проверяют практически.

Разрешите с доверенного phone LAN на выбранный узел:

- TCP **и** UDP `5060`: SIP signaling;
- UDP `10000–10199`: Asterisk RTP;
- только во время provisioning: UDP `69` **и** `20000–20031`: TFTP RRQ и transfer;
- DNS/NTP/DHCP к существующим серверам сети, не к новым Pod в этой схеме.

Ответы должны проходить обратно к динамическим клиентским UDP портам телефонов.
`69/udp` в одиночку недостаточно для TFTP. Порты `20000–20031` ограничивают
серверные transfer sockets; прошивайте сначала один телефон, затем небольшими
партиями. RTP диапазон — серверный; телефон выбирает собственный RTP source port.

Сопоставьте `deploy/firewall.nft.example` с действующим firewall узла. Сначала
`sudo nft -c -f …` проверяет синтаксис, затем владелец firewall вносит и сохраняет
правила своей штатной системой. **Не делайте `nft flush ruleset`**: это удалит
правила k3s. Шаблон разрешает LAN, но другие правила всё ещё могут блокировать
этот трафик. Поддержка NetworkPolicy для hostNetwork зависит от CNI, поэтому
обычная pod NetworkPolicy здесь не считается защитой. Не открывайте SIP/TFTP
на WAN, ZeroTier/WireGuard peer сетях или в домашнем Cloudflare tunnel.

DHCP остаётся на существующем роутере. В phone scope
задайте Cisco option **150 = 192.168.50.10** (IP TFTP), подтвердив выбранный IP.
Для dnsmasq это обычно `dhcp-option=tag:phones,150,192.168.50.10`, но тег `phones`
должен реально назначаться нужным MAC/диапазону: не вставляйте одну эту строку
в глобальную конфигурацию вслепую. При одном тестовом телефоне можно вручную
задать Alternate TFTP. Не запускайте второй DHCP server на LAN.

## 5. Доставить firmware на выбранный узел

На узле создаётся только каталог firmware, без конфигов с паролями. XML доставляет
Kubernetes Secret. Сначала подтвердите SSH host key привычным способом.

```bash
rsync -av --checksum .local/firmware/ voice-node:/tmp/cisco-firmware/
ssh voice-node 'sudo install -d -m 0755 /var/lib/cisco-tftp'
# Только после локальной проверки manifest; ожидаются обычные файлы в одном каталоге.
ssh voice-node 'sudo find /tmp/cisco-firmware -maxdepth 1 -type f -exec cp {} /var/lib/cisco-tftp/ \;'
ssh voice-node 'sudo find /var/lib/cisco-tftp -maxdepth 1 -type f -exec chmod 0644 {} \;'
```

Сверьте SHA-256 на узле с `.local/firmware-manifest.json`; наличие файла по имени
не доказывает правильную версию. Например `ssh voice-node 'sha256sum /var/lib/cisco-tftp/*'`.
Здесь нет `rsync --delete`: существующие файлы не удаляются незаметно. Перед новым
набором firmware вручную уберите/архивируйте устаревшие файлы, чтобы исключить
неверный load. Подкаталоги и symlink в наборе запрещены. Образы не содержат Cisco
firmware; после окончания работы локальная лицензированная копия остаётся у вас.

## 6. Отрендерить и применить

```bash
python3 -m unittest discover -s tests -v
bash scripts/deploy.sh --with-tftp
# По умолчанию это ТОЛЬКО локальная генерация, без kubectl.
```

Просмотрите `.local/rendered/core.yaml` и `tftp.yaml` приватно: там Secret, base64
не является шифрованием. Для первого server dry run требуется заранее созданный
namespace; сухая проверка Namespace не сохраняет его для проверки детей.

```bash
# Эти команды уже меняют только выделенный namespace, не запускают workloads.
kubectl --context home-k3s create namespace telephony
kubectl --context home-k3s label namespace telephony \
  pod-security.kubernetes.io/enforce=privileged \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/warn=restricted --overwrite
bash scripts/deploy.sh --server-dry-run --with-tftp
# После проверки firewall/socket/firmware, в согласованное окно:
bash scripts/deploy.sh --apply --firewall-reviewed --with-tftp
```

`hostNetwork` требует исключения из Pod Security baseline/restricted; privileged
label разрешает это на уровне **отдельного namespace**, но сам Asterisk
непривилегированный, UID 10001, без capabilities, с read-only rootfs. TFTP стартует
root только ради bind/chroot/setuid, с четырьмя необходимыми capabilities, затем
`tftpd-hpa` переключается на пользователя tftp. Не давайте посторонним право
создавать Pod в этом namespace. Kubernetes Secret требует нормального RBAC и
шифрования datastore/backups существующего кластера.

TFTP Job живёт максимум **60 минут**, затем завершается, через 10 минут удаляется
TTL controller. Запускайте первую прошивку сразу после готовности Job. Не начинайте
новую прошивку под конец часа. Для нового окна удалите старый Job, повторите apply
с `--with-tftp`. Не удаляйте Job и не прерывайте питание во время активной прошивки.

## 7. Приёмка и закрытие provisioning

```bash
kubectl --context home-k3s -n telephony get pods -o wide
kubectl --context home-k3s -n telephony logs job/cisco-tftp
kubectl --context home-k3s -n telephony exec deploy/asterisk -- asterisk -rx 'pjsip show endpoints'
kubectl --context home-k3s -n telephony exec deploy/asterisk -- asterisk -rx 'pjsip show contacts'
```

`tftpd-hpa` отправляет transfer logs в syslog. В минимальном образе syslog daemon
нет, поэтому `kubectl logs job/cisco-tftp` не является журналом всех RRQ/transfers.
Для краткой проверки сетевого обмена используйте на выбранном узле `tcpdump`
с фильтром UDP69/20000–20031 на LAN интерфейсе, без `-A`/`-X` и без сохранения
полного payload; завершите захват после проверки. Следите также за экраном телефона.

Проверьте один 7945G: корректный SIP load, адрес DHCP, верное время, регистрация.
Позвоните `600` (эхо), `601` (тестовый тон), затем между двумя аппаратами в обе
стороны. Поднимите/положите трубку, проверьте speakerphone, DTMF, hold/resume и
группу `620`; выдержите звонок несколько минут. После этого добавляйте остальные.
MAC/пароли/пакеты SIP в логах не публикуйте.

```bash
# Только когда все текущие flash/transfers завершены:
kubectl --context home-k3s -n telephony delete job cisco-tftp --ignore-not-found
# Затем уберите временный allow UDP69/20000–20031 в действующем firewall.
```

При следующей перезагрузке 7945G может снова запрашивать TFTP config; закрытый
TFTP означает отсутствие обновлений, а не гарантию автономного boot на каждой
firmware. Обязательно проверьте cold boot одного аппарата после закрытия окна.
Если выбранный load требует TFTP при boot, открывайте ограниченное окно на
перезагрузку; постоянный TFTP потребует отдельного осознанного решения и защиты LAN.

## Источники

- [Asterisk downloads / LTS](https://www.asterisk.org/downloads/).
- [Asterisk 22.11.0 source archive](https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-22.11.0.tar.gz).
- [Asterisk release support](https://docs.asterisk.org/About-the-Project/Asterisk-Versions/).
- [Kubernetes hostNetwork и NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/#networkpolicy-and-hostnetwork-pods).
- [Kubernetes Jobs: deadline, TTL](https://kubernetes.io/docs/concepts/workloads/controllers/job/).
- [tftpd-hpa: chroot, port-range, blocksize](https://manpages.debian.org/bookworm/tftpd-hpa/in.tftpd.8.en.html).
- [k3s networking](https://docs.k3s.io/networking/basic-network-options).
