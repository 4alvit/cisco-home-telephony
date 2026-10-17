# Образы с нативного GitHub runner

Обычные push/PR запускают сборку и smoke на стандартном `ubuntu-24.04`, без
публикации контейнеров и без загрузки artifacts. Если локальный компьютер ARM,
а выбранный k3s-узел AMD64, можно один раз получить уже проверенные AMD64-образы
через ручной workflow. Эмуляция на локальном ARM для этого не нужна.

```bash
gh workflow run validate.yml --repo 4alvit/cisco-home-telephony \
  --ref main -f export_images=true
gh run list --repo 4alvit/cisco-home-telephony --workflow validate.yml --limit 5
```

Выберите именно созданный вами run. Подтвердите его `headSha`, `event`,
`headBranch` и `conclusion` через `gh run view RUN_ID --json
headSha,event,headBranch,conclusion`. Нужны ожидаемый commit, `workflow_dispatch`,
`main` и `success`. Нельзя брать бинарники из непроверенного PR или чужого fork.

```bash
umask 077
mkdir -p .local/ci-images
gh run download RUN_ID --repo 4alvit/cisco-home-telephony \
  --name verified-amd64-images-COMMIT_SHA --dir .local/ci-images
cd .local/ci-images
# Linux; на macOS вместо sha256sum используйте shasum -a 256.
sha256sum -c SHA256SUMS
cat source.json
docker load --input asterisk-amd64.tar.gz
docker load --input tftp-amd64.tar.gz
docker image inspect cisco-home-ci/asterisk:22.11.0 cisco-home-ci/tftp:bookworm
```

Сверьте `source.json` с run/commit и `images.json` с загруженными image ID и
`linux/amd64`. В artifacts нет домашних конфигураций, firmware или секретов.
Теги `cisco-home-ci/*` локальные: перед push назначьте теги своего registry и
следуйте [deployment runbook](k3s-deployment.md). В k3s используйте итоговые
registry digests, а не image ID или изменяемые теги.

Если домашний registry использует HTTP, можно обойти изменение Docker daemon:
распаковать `.tar.gz`, затем использовать `skopeo copy --dest-tls-verify=false
docker-archive:/absolute/path/asterisk-amd64.tar
docker://YOUR_REGISTRY/cisco-home/asterisk:22.11.0`. Исключение TLS относится
только к этому подтверждённому внутреннему registry. Проверьте manifest/config
digest и архитектуру в registry после переноса.

Экспорт выключен по умолчанию; размер ограничен 400 MiB, retention — один день.
Хранение artifacts имеет отдельную общую квоту: бесплатные runner minutes не
означают неограниченное хранение. После успешного скачивания и проверки удалите
именно artifact своего run в GitHub Actions. Для первого домашнего deployment
artifact был удалён сразу после проверенного переноса обоих образов.
