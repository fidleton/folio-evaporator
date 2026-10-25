FROM ghcr.io/home-assistant/devcontainer:6-apps

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/folio-venv \
    && chown -R vscode:vscode /opt/folio-venv

ENV PATH="/opt/folio-venv/bin:${PATH}"
