# Multi-stage: build the static React bundle with Node, then serve it with
# a minimal nginx image -- the build tooling (Vite, npm, node_modules)
# never needs to exist in the final image. See docker-compose.yml,
# DECISIONS.md (issue #52).
#
# Build context is `frontend/` itself (see docker-compose.yml's
# `build.context`), not the repo root -- this Dockerfile only ever needs
# frontend/'s own files.

FROM node:22-alpine AS build

WORKDIR /app

# package*.json copied and installed before the rest of the source tree,
# so editing component code doesn't invalidate this (expensive) layer.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Baked into the static bundle at build time -- see vite.config.js's own
# comment on why a Docker build can't use its usual dev-time
# backend_port.txt discovery. Passed through from docker-compose.yml's
# build.args, itself derived from HOST_BACKEND_PORT.
ARG VITE_API_BASE_URL=http://localhost:8080/api
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
