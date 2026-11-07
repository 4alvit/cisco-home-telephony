# Build only through scripts/build-voice-image.sh, which supplies a digest and
# an allowlisted context. No private checkout files enter the build context.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ARG PYTHON_IMAGE
LABEL org.opencontainers.image.base.name="${PYTHON_IMAGE}" \
      io.cisco-home-telephony.debian.snapshot="20261101T000000Z"
# Bound archive retries and stalled connections for every APT operation.
RUN printf '%s\n' \
       'Acquire::Retries "2";' \
       'Acquire::http::Timeout "30";' \
       'Acquire::https::Timeout "30";' \
       'Acquire::http::Pipeline-Depth "0";' \
       'Acquire::https::Pipeline-Depth "0";' \
       'Acquire::http::Dl-Limit "1000";' \
       'Acquire::https::Dl-Limit "1000";' \
       > /etc/apt/apt.conf.d/zz-cisco-network

# Freeze Debian package indexes as well as Python wheels. Updating this date is
# an explicit dependency update; the base digest is recorded by the helper.
# Indexes stay on the snapshot mirrors. The last mirror serves only .deb payloads;
# APT still verifies the same Debian Release signatures and package checksums.
RUN grep -qx 'VERSION_CODENAME=trixie' /etc/os-release \
    && rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
    && printf '%s\tpriority:1\n%s\tpriority:2\n%s\tpriority:3 type:deb\n' \
       'https://snapshot.debian.org/archive/debian/20261101T000000Z/' \
       'https://snapshot-cloudflare.debian.org/archive/debian/20261101T000000Z/' \
       'https://deb.debian.org/debian/' \
       > /etc/apt/cisco-debian-snapshot.mirrors \
    && printf '%s\tpriority:1\n%s\tpriority:2\n%s\tpriority:3 type:deb\n' \
       'https://snapshot.debian.org/archive/debian-security/20261101T000000Z/' \
       'https://snapshot-cloudflare.debian.org/archive/debian-security/20261101T000000Z/' \
       'https://deb.debian.org/debian-security/' \
       > /etc/apt/cisco-security-snapshot.mirrors \
    && printf '%s\n' \
       'deb [check-valid-until=no] mirror+file:/etc/apt/cisco-debian-snapshot.mirrors trixie main' \
       'deb [check-valid-until=no] mirror+file:/etc/apt/cisco-debian-snapshot.mirrors trixie-updates main' \
       'deb [check-valid-until=no] mirror+file:/etc/apt/cisco-security-snapshot.mirrors trixie-security main' \
       > /etc/apt/sources.list \
    && apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tzdata \
       ffmpeg=7:7.1.5-0+deb13u1 libmbedcrypto16=3.6.6-0.1~deb13u1 \
       libbrotli1=1.1.0-2+deb13u1 libexpat1=2.9.0-1~deb13u1 \
       libssh-4=0.11.5-0+deb13u1 libssl3t64=3.5.7-1~deb13u3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 cisco-voice \
    && useradd --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin cisco-voice \
    && mkdir -p /run/voice/.local/config /run/voice/.local/state /workspace \
    && chown -R 10001:10001 /run/voice \
    && chmod 0700 /run/voice /run/voice/.local /run/voice/.local/config /run/voice/.local/state

# DSA-6613-1: upgrade only zlib from the reviewed security snapshot.
# Keep the original multimedia snapshot and its exact package pins unchanged.
RUN printf '%s\tpriority:1\n%s\tpriority:2\n%s\tpriority:3 type:deb\n' \
       'https://snapshot.debian.org/archive/debian-security/20261107T160000Z/' \
       'https://snapshot-cloudflare.debian.org/archive/debian-security/20261107T160000Z/' \
       'https://deb.debian.org/debian-security/' \
       > /etc/apt/cisco-zlib-security.mirrors \
    && printf '%s\n' \
       'deb [check-valid-until=no] mirror+file:/etc/apt/cisco-zlib-security.mirrors trixie-security main' \
       > /tmp/cisco-zlib-security.list \
    && apt-get -o Dir::Etc::sourcelist=/tmp/cisco-zlib-security.list -o Dir::Etc::sourceparts=- update \
    && apt-get -o Dir::Etc::sourcelist=/tmp/cisco-zlib-security.list -o Dir::Etc::sourceparts=- \
       install -y --no-install-recommends --only-upgrade zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
    && test "$(dpkg-query -W -f='${Version}' zlib1g)" = '1:1.3.dfsg+really1.3.1-1+deb13u1' \
    && rm -f /tmp/cisco-zlib-security.list /etc/apt/cisco-zlib-security.mirrors \
    && rm -rf /var/lib/apt/lists/*
LABEL io.cisco-home-telephony.zlib-security.snapshot="20261107T160000Z"

COPY containers/voice/requirements.lock /opt/voice/requirements.lock
COPY containers/voice/install_locked.py /tmp/install_locked.py
RUN python3 -I /tmp/install_locked.py /opt/voice/requirements.lock \
    && rm /tmp/install_locked.py

COPY scripts/voice_session.py scripts/voice_session_sip.py /workspace/scripts/
COPY containers/asterisk/phone_voice_transport.py containers/asterisk/phone_voice_controller.py containers/asterisk/phone_voice_control.py containers/asterisk/phone_voice_hands_free.py containers/asterisk/phone_voice_display.py containers/asterisk/phone_camera_preview.py containers/asterisk/phone_gateway.py containers/asterisk/phone_apps.py containers/asterisk/phone_apps_ha.py /workspace/containers/asterisk/
COPY containers/asterisk/phone_voice_sip.py containers/asterisk/phone_voice_sip_ami.py /workspace/containers/asterisk/
COPY containers/asterisk/phone_voice_recovery.py containers/asterisk/phone_voice_quiescence.py /workspace/containers/asterisk/
COPY containers/asterisk/phone_player.py containers/asterisk/phone_notifications.py containers/asterisk/media_runtime.py containers/asterisk/radio.py containers/asterisk/radio-catalog.json /workspace/containers/asterisk/
COPY voice_assistant/__init__.py voice_assistant/config.py voice_assistant/audio.py voice_assistant/service.py voice_assistant/ha.py voice_assistant/pipeline.py voice_assistant/playback.py /workspace/voice_assistant/
COPY containers/voice/smoke.py /opt/voice/smoke.py
COPY containers/voice/stage_private.py /opt/voice/stage_private.py

ENV PYTHONPATH=/workspace:/workspace/containers/asterisk \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TMPDIR=/tmp
WORKDIR /workspace
USER 10001:10001
# Starting the image without an explicit live command is harmless.
CMD ["python3", "/workspace/scripts/voice_session.py", "--help"]
