FROM python:3.12-slim
# The gateway is pure stdlib asyncio now, so nothing is pip-installed. dnsmasq
# is the DNS sinkhole; openssl is kept for future TLS-terminating canned
# responses.
RUN apt-get update && apt-get install -y --no-install-recommends openssl dnsmasq \
    && rm -rf /var/lib/apt/lists/*
COPY gateway.py /opt/gateway.py
COPY start.sh /opt/start.sh
RUN chmod 0755 /opt/start.sh
EXPOSE 8080 8443 53/udp
CMD ["/opt/start.sh"]
