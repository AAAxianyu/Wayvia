FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
ARG TARGETARCH
ARG UBUNTU_APT_SNAPSHOT=20261014T064100Z
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && printf 'APT::Snapshot "%s";\nAcquire::Snapshots::URI::Host::ports.ubuntu.com "https://snapshot.ubuntu.com/ubuntu/@SNAPSHOTID@/";\n' "$UBUNTU_APT_SNAPSHOT" > /etc/apt/apt.conf.d/50snapshot \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl iproute2 nftables openvpn=2.6.19-0ubuntu0.24.04.4 openssl python3 python3-venv python3-yaml tcpdump procps dnsutils iputils-ping \
    && rm -rf /var/lib/apt/lists/*
COPY .cache/components/${TARGETARCH}/sing-box .cache/components/${TARGETARCH}/mihomo .cache/components/${TARGETARCH}/v2raya .cache/components/${TARGETARCH}/v2raya_core .cache/components/${TARGETARCH}/v2ray /usr/local/bin/
COPY .cache/components/${TARGETARCH}/geoip.dat .cache/components/${TARGETARCH}/geosite.dat /usr/local/share/v2raya/
COPY .cache/lab-python/${TARGETARCH}/ /opt/wayvia-lab-wheels/
RUN python3 -m venv /opt/wayvia-lab-python && /opt/wayvia-lab-python/bin/pip install --no-index --require-hashes --find-links /opt/wayvia-lab-wheels -r /opt/wayvia-lab-wheels/requirements.txt
COPY deploy/lab/ /opt/wayvia-lab/
ENTRYPOINT ["python3", "/opt/wayvia-lab/run.py"]
