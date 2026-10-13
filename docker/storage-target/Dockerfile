FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# tgt + targetcli-fb give us the *real* LIO/tgt stack when the container is
# privileged and the host kernel exposes the iSCSI target modules. When they
# are unavailable the sim falls back to `mode: simulated`.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        curl \
        ca-certificates \
        procps \
        kmod \
        tgt \
        targetcli-fb \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/lib/labbench

COPY docker/storage-target/iscsi_sim.py /opt/labbench/iscsi_sim.py

EXPOSE 8080 3260

CMD ["python3", "/opt/labbench/iscsi_sim.py", "--host", "0.0.0.0", "--port", "8080"]
