# Build only through scripts/build-voice-image.sh, which supplies a digest and
# an allowlisted context. No private checkout files enter the build context.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ARG PYTHON_IMAGE
LABEL org.opencontainers.image.base.name="${PYTHON_IMAGE}" \
      io.cisco-home-telephony.debian.snapshot="20261101T000000Z"

# Freeze Debian package indexes as well as Python wheels. Updating this date is
# an explicit dependency update; the base digest is recorded by the helper.
RUN grep -qx 'VERSION_CODENAME=trixie' /etc/os-release \
    && rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261101T000000Z trixie main' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261101T000000Z trixie-updates main' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261101T000000Z trixie-security main' \
       > /etc/apt/sources.list \
    && apt-get -o Acquire::Retries=3 update \
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
