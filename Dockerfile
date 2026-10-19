FROM node:22-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ARG BUILD_VERSION=dev
ARG BUILD_ARCH=amd64
LABEL io.hass.name="Folio Evaporator" \
	io.hass.description="See your portfolio go up in smoke" \
	io.hass.type="app" \
	io.hass.url="https://github.com/fidleton/folio-evaporator" \
	io.hass.version="${BUILD_VERSION}" \
	io.hass.arch="${BUILD_ARCH}" \
	org.opencontainers.image.title="Folio Evaporator" \
	org.opencontainers.image.description="See your portfolio go up in smoke" \
	org.opencontainers.image.licenses="AGPL-3.0"
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data FRONTEND_DIST=/app/frontend/dist
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=frontend-build /frontend/dist ./frontend/dist
EXPOSE 8099
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099", "--proxy-headers", "--forwarded-allow-ips", "*"]