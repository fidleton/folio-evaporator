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
LABEL io.hass.name="Folio Portfolio Tracker" \
	io.hass.description="Track stock portfolios across broker accounts" \
	io.hass.type="app" \
	io.hass.version="${BUILD_VERSION}" \
	io.hass.arch="${BUILD_ARCH}"
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data FRONTEND_DIST=/app/frontend/dist
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=frontend-build /frontend/dist ./frontend/dist
EXPOSE 8099
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099", "--proxy-headers", "--forwarded-allow-ips", "*"]