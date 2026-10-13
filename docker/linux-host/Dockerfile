FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    LABBENCH_SIMULATOR=http://storage-target:8080

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server \
        python3 \
        iproute2 \
        iputils-ping \
        curl \
        ca-certificates \
        procps \
        kmod \
        open-iscsi \
        multipath-tools \
        sg3-utils \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /run/sshd /mnt/labbench \
    && echo 'root:labbench' | chpasswd \
    && sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?UsePAM.*/UsePAM no/' /etc/ssh/sshd_config

COPY docker/linux-host/sim_agent.py /opt/labbench/sim_agent.py
COPY docker/linux-host/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 22

CMD ["/usr/local/bin/entrypoint.sh"]
