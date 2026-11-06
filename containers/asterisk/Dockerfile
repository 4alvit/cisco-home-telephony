# syntax=docker/dockerfile:1
# Supply a verified Debian image digest; scripts/build-images.sh resolves it.
ARG DEBIAN_IMAGE
FROM ${DEBIAN_IMAGE} AS distribution
ARG DEBIAN_IMAGE
LABEL org.opencontainers.image.base.name="${DEBIAN_IMAGE}" \
      io.cisco-home-telephony.debian.snapshot="20261101T000000Z"
# Bound archive retries and stalled connections in this stage and its descendants.
RUN printf '%s\n' \
       'Acquire::Retries "6";' \
       'Acquire::http::Timeout "30";' \
       'Acquire::https::Timeout "30";' \
       'Acquire::http::Pipeline-Depth "0";' \
       'Acquire::https::Pipeline-Depth "0";' \
       > /etc/apt/apt.conf.d/zz-cisco-network
# Both compilation and runtime resolve packages from the same reviewed snapshot.
# Debian slim has no CA bundle. Bootstrap it from the primary archive over HTTP;
# APT still verifies Debian Release signatures and package checksums. After that,
# HTTPS mirror lists allow per-file failover from the primary archive to the CDN.
# Keep six retries for CA bootstrap, then use two so mirror failover happens sooner.
RUN grep -qx 'VERSION_CODENAME=trixie' /etc/os-release \
    && rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
       'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261101T000000Z trixie main' \
       'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261101T000000Z trixie-updates main' \
       'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20261101T000000Z trixie-security main' \
       > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && printf '%s\tpriority:1\n%s\tpriority:2\n' \
       'https://snapshot.debian.org/archive/debian/20261101T000000Z/' \
       'https://snapshot-cloudflare.debian.org/archive/debian/20261101T000000Z/' \
       > /etc/apt/cisco-debian-snapshot.mirrors \
    && printf '%s\tpriority:1\n%s\tpriority:2\n' \
       'https://snapshot.debian.org/archive/debian-security/20261101T000000Z/' \
       'https://snapshot-cloudflare.debian.org/archive/debian-security/20261101T000000Z/' \
       > /etc/apt/cisco-security-snapshot.mirrors \
    && printf '%s\n' \
       'deb [check-valid-until=no] mirror+file:/etc/apt/cisco-debian-snapshot.mirrors trixie main' \
       'deb [check-valid-until=no] mirror+file:/etc/apt/cisco-debian-snapshot.mirrors trixie-updates main' \
       'deb [check-valid-until=no] mirror+file:/etc/apt/cisco-security-snapshot.mirrors trixie-security main' \
       > /etc/apt/sources.list \
    && printf '%s\n' \
       'Acquire::Retries "2";' \
       'Acquire::http::Timeout "30";' \
       'Acquire::https::Timeout "30";' \
       'Acquire::http::Pipeline-Depth "0";' \
       'Acquire::https::Pipeline-Depth "0";' \
       > /etc/apt/apt.conf.d/zz-cisco-network \
    && rm -rf /var/lib/apt/lists/*
# Debian's Pillow package still carries reviewed critical findings. Stage the
# hash-locked upstream wheel with the SAME Debian interpreter as the runtime.
# Installers and their dependencies exist only in this disposable build stage.
FROM distribution AS pillow-build
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*
COPY pillow.lock /tmp/pillow.lock
RUN python3 -I - <<'PYINSTALL'
from pathlib import Path
import subprocess
import sys
import sysconfig
assert sys.version_info[:2] == (3, 13), 'Reviewed Pillow wheel requires CPython 3.13'
site = Path(sysconfig.get_path('platlib'))
assert site.is_relative_to('/usr/local/lib') and str(site) in sys.path, 'Unexpected Debian import path'
stage = Path('/pillow-stage') / site.relative_to('/')
subprocess.run([sys.executable, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check',
                'install', '--no-cache-dir', '--no-deps', '--no-compile', '--only-binary=:all:',
                '--require-hashes', '--target', str(stage), '-r', '/tmp/pillow.lock'], check=True)
PYINSTALL
FROM distribution AS build
ARG ASTERISK_VERSION=22.11.0
ARG ASTERISK_SHA256=3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl pkg-config python3 bzip2 \
    libedit-dev libssl-dev libxml2-dev uuid-dev libsqlite3-dev libncurses-dev libunbound-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fL --retry 3 "https://downloads.asterisk.org/pub/telephony/asterisk/old-releases/asterisk-${ASTERISK_VERSION}.tar.gz" -o asterisk.tar.gz \
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
FROM distribution
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    ca-certificates tzdata libedit2 libxml2 libuuid1 libsqlite3-0 libncurses6 libopus0 libunbound8 python3 \
    ffmpeg=7:7.1.5-0+deb13u1 libmbedcrypto16=3.6.6-0.1~deb13u1 \
    libbrotli1=1.1.0-2+deb13u1 libexpat1=2.9.0-1~deb13u1 \
    libssh-4=0.11.5-0+deb13u1 libssl3t64=3.5.7-1~deb13u3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 asterisk && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin asterisk
COPY --from=build /stage/ /
COPY --from=pillow-build /pillow-stage/ /
COPY pillow.lock /usr/share/cisco-home-telephony/pillow.lock
# Validate the staged extension with the real final interpreter, offline and
# isolated. No PYTHONPATH, activation script or interpreter wrapper is needed.
RUN --network=none python3 -I - <<'PYCHECK'
import importlib.metadata
import importlib.util
from pathlib import Path
import sysconfig
import PIL
from PIL import Image
assert importlib.metadata.version('Pillow') == PIL.__version__ == '13.0.0'
assert Path(PIL.__file__).is_relative_to(sysconfig.get_path('platlib'))
assert all(importlib.util.find_spec(name) is None for name in ('pip', 'ensurepip'))
assert Image.new('RGB', (8, 8), (10, 20, 30)).resize((4, 4)).getpixel((0, 0)) == (10, 20, 30)
PYCHECK
RUN --network=none python3 -c "import PIL; from PIL import Image; assert PIL.__version__ == '13.0.0'; assert Image.new('RGB', (2, 2)).size == (2, 2)"
COPY radio.py phone_services.py dial_audio.py radio-catalog.json /var/lib/asterisk/agi-bin/
COPY phone_gateway.py /usr/local/lib/cisco-phone-gateway.py
COPY phone_apps.py phone_apps_ha.py phone_camera_preview.py phone_voice_display.py phone_notifications.py /usr/local/lib/
COPY phone_player.py media_runtime.py radio.py radio-catalog.json /usr/local/lib/
RUN chmod 0555 /var/lib/asterisk/agi-bin/radio.py /var/lib/asterisk/agi-bin/phone_services.py \
    /var/lib/asterisk/agi-bin/dial_audio.py \
    && chmod 0444 /var/lib/asterisk/agi-bin/radio-catalog.json
RUN mkdir -p /run/asterisk /var/lib/asterisk/db /var/spool/asterisk /var/log/asterisk \
    && chown -R 10001:10001 /run/asterisk /var/lib/asterisk /var/spool/asterisk /var/log/asterisk
USER 10001:10001
EXPOSE 5060/udp 5060/tcp 10000-10199/udp
ENTRYPOINT ["/usr/sbin/asterisk"]
CMD ["-f", "-vv", "-C", "/etc/asterisk/asterisk.conf"]
