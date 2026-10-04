FROM python:3.12-slim
RUN pip install --no-cache-dir 'aiohttp==3.*' && \
    apt-get update && apt-get install -y --no-install-recommends openssl dnsmasq && \
    rm -rf /var/lib/apt/lists/*
COPY gateway.py /opt/gateway.py
COPY start.sh /opt/start.sh
RUN chmod 0755 /opt/start.sh
EXPOSE 8080 8443 53/udp
CMD ["/opt/start.sh"]
