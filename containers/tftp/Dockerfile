# syntax=docker/dockerfile:1
ARG DEBIAN_IMAGE
FROM ${DEBIAN_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends tftpd-hpa tftp-hpa ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/* && mkdir -p /srv/tftp
COPY entrypoint.sh /usr/local/bin/cisco-tftp
COPY runtime.py /usr/local/lib/cisco-tftp/runtime.py
COPY managed.rules /usr/local/etc/cisco-tftp.rules
RUN chmod 0755 /usr/local/bin/cisco-tftp \
    && chmod 0444 /usr/local/etc/cisco-tftp.rules
EXPOSE 69/udp 20000-20031/udp
ENTRYPOINT ["/usr/local/bin/cisco-tftp"]
CMD ["--foreground", "--user", "tftp", "--secure", "--address", "0.0.0.0:69", "--port-range", "20000:20031", "--blocksize", "1468", "/srv/tftp"]
