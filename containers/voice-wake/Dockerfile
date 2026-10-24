# An optional layer above the immutable, independently verified manual image.
# Build with scripts/build-voice-wake-image.sh; never send the checkout as context.
ARG VOICE_IMAGE
FROM ${VOICE_IMAGE}
USER root
COPY containers/voice-wake/requirements.lock /opt/voice-wake/requirements.lock
COPY containers/voice/install_locked.py /tmp/install_locked.py
RUN python3 -I /tmp/install_locked.py /opt/voice-wake/requirements.lock \
    && rm /tmp/install_locked.py
COPY voice_assistant/wake.py /workspace/voice_assistant/wake.py
COPY containers/voice-wake/smoke.py /opt/voice-wake/smoke.py
COPY containers/voice-wake/stage_models.py /opt/voice-wake/stage_models.py
COPY containers/voice-wake/run_reserved.py /opt/voice-wake/run_reserved.py
COPY base-source.json /opt/voice-wake/base-source.json
USER 10001:10001
# Inherit the manual image's harmless --help command, paths and environment.
