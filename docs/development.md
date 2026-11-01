# Development

Open the repository in VS Code and choose **Reopen in Container**. The devcontainer uses Home Assistant's `6-apps` development image, starts its systemd-managed nested Docker service, and mounts this repository into `/mnt/supervisor/apps/local/folio-evaporator` for local Supervisor testing. It adds Python 3.13 and Node 22 and installs backend/frontend dependencies.

Use **Terminal: Run Task → Start Home Assistant** or run `supervisor_run` in a devcontainer terminal. The local Supervisor downloads and starts Home Assistant and exposes the app store. Add this repository as a local add-on, install Folio, and open it through Ingress at `http://localhost:7123`.

This is a development Supervisor environment, not a production Home Assistant Supervised installation. Supervisor may report the devcontainer's Debian host as unsupported and disable host-specific functions; production support remains Home Assistant OS and Supervised on their supported hosts.

Forwarded ports:

- `7123`: Home Assistant (`8123` in the devcontainer).
- `7180`: Home Assistant HTTP proxy (`80` in the devcontainer).
- `7357`: Supervisor diagnostics (`4357` in the devcontainer).

Run backend tests, frontend lint/build, and the add-on image build in the devcontainer:

```sh
python -m pytest
npm --prefix frontend run lint
npm --prefix frontend run build
docker build --build-arg BUILD_ARCH=amd64 -t folio-tracker:local .
```

For local development, run the API and UI in separate devcontainer terminals:

```sh
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8099
npm --prefix frontend run dev -- --host 0.0.0.0
```

Vite proxies `/api` to the API service. Devcontainer ports 5173 and 8099 are forwarded.
