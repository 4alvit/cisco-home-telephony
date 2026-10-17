# syntax=docker/dockerfile:1
ARG DEBIAN_IMAGE
FROM ${DEBIAN_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends tftpd-hpa tftp-hpa ca-certificates \
    && rm -rf /var/lib/apt/lists/* && mkdir -p /srv/tftp
EXPOSE 69/udp 20000-20031/udp
ENTRYPOINT ["/usr/sbin/in.tftpd"]
CMD ["--foreground", "--user", "tftp", "--secure", "--address", "0.0.0.0:69", "--port-range", "20000:20031", "--blocksize", "1468", "/srv/tftp"]
