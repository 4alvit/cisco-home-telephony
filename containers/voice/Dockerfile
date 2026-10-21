# Build only through scripts/build-voice-image.sh, which supplies a digest and
# an allowlisted context. No private checkout files enter the build context.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}

# Freeze Debian package indexes as well as Python wheels. Updating this date is
# an explicit dependency update; the base digest is recorded by the helper.
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261019T000000Z bookworm main' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261019T000000Z bookworm-updates main' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261019T000000Z bookworm-security main' \
       > /etc/apt/sources.list \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 cisco-voice \
    && useradd --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin cisco-voice \
    && mkdir -p /run/voice/.local/config /run/voice/.local/state /workspace \
    && chown -R 10001:10001 /run/voice \
    && chmod 0700 /run/voice /run/voice/.local /run/voice/.local/config /run/voice/.local/state

COPY containers/voice/requirements.lock /opt/voice/requirements.lock
RUN python3 -m pip --isolated install --no-cache-dir --disable-pip-version-check \
    --only-binary=:all: --require-hashes -r /opt/voice/requirements.lock \
    && python3 -m pip check

COPY scripts/voice_session.py scripts/voice_session_sip.py /workspace/scripts/
COPY containers/asterisk/phone_voice_transport.py containers/asterisk/phone_voice_controller.py containers/asterisk/phone_voice_control.py containers/asterisk/phone_voice_hands_free.py containers/asterisk/phone_voice_display.py containers/asterisk/phone_camera_preview.py containers/asterisk/phone_gateway.py containers/asterisk/phone_apps.py containers/asterisk/phone_apps_ha.py /workspace/containers/asterisk/
COPY containers/asterisk/phone_voice_sip.py containers/asterisk/phone_voice_sip_ami.py /workspace/containers/asterisk/
COPY voice_assistant/__init__.py voice_assistant/config.py voice_assistant/audio.py voice_assistant/service.py voice_assistant/ha.py voice_assistant/pipeline.py voice_assistant/playback.py /workspace/voice_assistant/
COPY containers/voice/smoke.py /opt/voice/smoke.py
COPY containers/voice/stage_private.py /opt/voice/stage_private.py

ENV PYTHONPATH=/workspace:/workspace/containers/asterisk \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TMPDIR=/tmp
WORKDIR /workspace
USER 10001:10001
# Starting the image without an explicit live command is harmless.
CMD ["python3", "/workspace/scripts/voice_session.py", "--help"]
