FROM debian:12-slim

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openvpn iproute2 iputils-ping procps iptables curl openssl ca-certificates python3 python3-requests \
 && rm -rf /var/lib/apt/lists/*

COPY e2e.py /e2e.py
CMD ["python3", "-u", "/e2e.py"]
