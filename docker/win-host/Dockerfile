FROM mcr.microsoft.com/powershell:7.4-ubuntu-22.04

ENV DEBIAN_FRONTEND=noninteractive \
    LABBENCH_SIMULATOR=http://storage-target:8080 \
    LABBENCH_WINRM_SHELL=/bin/bash

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        curl \
        ca-certificates \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Make `powershell` / `powershell.exe` resolvable, which is what pywinrm and
# ansible.windows expect to invoke.
RUN ln -sf /opt/microsoft/powershell/7/pwsh /usr/local/bin/powershell \
    && ln -sf /opt/microsoft/powershell/7/pwsh /usr/local/bin/powershell.exe \
    && mkdir -p /var/labbench/mnt

COPY docker/win-host/winrm_stub.py /opt/labbench/winrm_stub.py
COPY docker/win-host/SimAgent.ps1 /opt/labbench/SimAgent.ps1

EXPOSE 5985

CMD ["python3", "/opt/labbench/winrm_stub.py", "--host", "0.0.0.0", "--port", "5985"]
