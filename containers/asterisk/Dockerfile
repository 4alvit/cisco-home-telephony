# syntax=docker/dockerfile:1
# Supply a verified Debian image digest; scripts/build-images.sh resolves it.
ARG DEBIAN_IMAGE
FROM ${DEBIAN_IMAGE} AS build
ARG ASTERISK_VERSION=22.11.0
ARG ASTERISK_SHA256=3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl pkg-config python3 bzip2 \
    libedit-dev libssl-dev libxml2-dev uuid-dev libsqlite3-dev libncurses-dev \
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
# Debian has /var/run -> /run. Runtime directories are created in the final image.
RUN rm -rf /stage/var/run
FROM ${DEBIAN_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libedit2 libssl3 libxml2 libuuid1 libsqlite3-0 libncurses6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 asterisk && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin asterisk
COPY --from=build /stage/ /
RUN mkdir -p /run/asterisk /var/lib/asterisk/db /var/spool/asterisk /var/log/asterisk \
    && chown -R 10001:10001 /run/asterisk /var/lib/asterisk /var/spool/asterisk /var/log/asterisk
USER 10001:10001
EXPOSE 5060/udp 5060/tcp 10000-10199/udp
ENTRYPOINT ["/usr/sbin/asterisk"]
CMD ["-f", "-vv", "-C", "/etc/asterisk/asterisk.conf"]
