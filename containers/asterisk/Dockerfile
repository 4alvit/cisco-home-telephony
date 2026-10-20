# syntax=docker/dockerfile:1
# Supply a verified Debian image digest; scripts/build-images.sh resolves it.
ARG DEBIAN_IMAGE
FROM ${DEBIAN_IMAGE} AS build
ARG ASTERISK_VERSION=22.11.0
ARG ASTERISK_SHA256=3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl pkg-config python3 bzip2 \
    libedit-dev libssl-dev libxml2-dev uuid-dev libsqlite3-dev libncurses-dev libunbound-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fL --retry 3 "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.tar.gz" -o asterisk.tar.gz \
    && printf '%s  asterisk.tar.gz\n' "$ASTERISK_SHA256" | sha256sum -c - \
    && tar -xzf asterisk.tar.gz --strip-components=1 \
    && ./configure --with-pjproject-bundled --with-jansson-bundled \
         --without-dahdi --without-pri --without-pgsql --without-mysqlclient \
         --without-odbc --without-radius --without-alsa --without-portaudio \
         --without-jack --without-speex --without-speexdsp --without-ogg --without-vorbis \
    && make menuselect.makeopts \
    && menuselect/menuselect --disable BUILD_NATIVE \
         --disable-category MENUSELECT_CORE_SOUNDS --disable-category MENUSELECT_MOH menuselect.makeopts \
    && make -j2 \
    && make install DESTDIR=/stage
RUN test -f /stage/usr/lib/asterisk/modules/app_audiosocket.so \
    && test -f /stage/usr/lib/asterisk/modules/chan_audiosocket.so \
    && test -f /stage/usr/lib/asterisk/modules/codec_g722.so \
    && test -f /stage/usr/lib/asterisk/modules/codec_resample.so \
    && test -f /stage/usr/lib/asterisk/modules/app_signal.so \
    && test -f /stage/usr/lib/asterisk/modules/res_audiosocket.so \
    && test -f /stage/usr/lib/asterisk/modules/res_agi.so \
    && test -f /stage/usr/lib/asterisk/modules/res_resolver_unbound.so
# Debian has /var/run -> /run. Runtime directories are created in the final image.
RUN rm -rf /stage/var/run
# Open-source Opus transcoder (GPL-2.0); Asterisk's Opus format alone is pass-through.
# Build only the codec against these exact Asterisk headers, retaining Asterisk's
# own res_format_attr_opus module. Keep corresponding source/license in the image.
ARG OPUS_SOURCE_COMMIT=591b6cd9b75e2b9b6d815ef364a385fccee595a7
ARG OPUS_SOURCE_SHA256=4b9b6b7e05d1450bf88932fdac280d210f6209c4fefed8b7c82995fbc869ed70
RUN apt-get update && apt-get install -y --no-install-recommends libopus-dev \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fL --retry 3 "https://github.com/wazo-platform/wazo-codec-opus-open-source/archive/${OPUS_SOURCE_COMMIT}.tar.gz" -o /tmp/opus-source.tar.gz \
    && printf '%s  /tmp/opus-source.tar.gz\n' "$OPUS_SOURCE_SHA256" | sha256sum -c - \
    && mkdir /build/opus-source \
    && tar -xzf /tmp/opus-source.tar.gz -C /build/opus-source --strip-components=1 \
    && make -C /build/opus-source codec_opus_open_source CPATH=-I/build/include \
         CFLAGS='-pthread -D_FORTIFY_SOURCE=2 -fPIC -Wl,--no-as-needed' \
    && install -m 0755 /build/opus-source/codecs/codec_opus_open_source.so /stage/usr/lib/asterisk/modules/ \
    && mkdir -p /stage/usr/share/doc/asterisk-opus-open-source \
    && cp /build/opus-source/LICENSE /build/opus-source/README.md /stage/usr/share/doc/asterisk-opus-open-source/ \
    && cp /tmp/opus-source.tar.gz /stage/usr/share/doc/asterisk-opus-open-source/source.tar.gz
FROM ${DEBIAN_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libedit2 libssl3 libxml2 libuuid1 libsqlite3-0 libncurses6 libopus0 libunbound8 python3 python3-pil ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 asterisk && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin asterisk
COPY --from=build /stage/ /
COPY radio.py phone_services.py radio-catalog.json /var/lib/asterisk/agi-bin/
COPY phone_gateway.py /usr/local/lib/cisco-phone-gateway.py
COPY phone_apps.py phone_apps_ha.py phone_camera_preview.py phone_voice_display.py /usr/local/lib/
RUN chmod 0555 /var/lib/asterisk/agi-bin/radio.py /var/lib/asterisk/agi-bin/phone_services.py \
    && chmod 0444 /var/lib/asterisk/agi-bin/radio-catalog.json
RUN mkdir -p /run/asterisk /var/lib/asterisk/db /var/spool/asterisk /var/log/asterisk \
    && chown -R 10001:10001 /run/asterisk /var/lib/asterisk /var/spool/asterisk /var/log/asterisk
USER 10001:10001
EXPOSE 5060/udp 5060/tcp 10000-10199/udp
ENTRYPOINT ["/usr/sbin/asterisk"]
CMD ["-f", "-vv", "-C", "/etc/asterisk/asterisk.conf"]
