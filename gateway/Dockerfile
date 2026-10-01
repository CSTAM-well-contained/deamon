# Gateway image: HAProxy + Keepalived + gw-agent — locally this container IS a gateway VM.
# Responsible for: building the Rust agent and packaging it with HAProxy, Keepalived and their scripts.
# NOT responsible for: any runtime decision (entrypoint.sh starts things, gw-agent manages them).
# Criteria 2 + 4. Build context = repository root.

FROM rust:1-bookworm AS build
WORKDIR /src
COPY agent/Cargo.toml agent/Cargo.lock ./
COPY agent/src ./src
RUN cargo build --release --locked

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends haproxy keepalived curl iproute2 procps tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/gw-agent /usr/local/bin/gw-agent
COPY --chmod=755 gateway/keepalived/check_haproxy.sh gateway/keepalived/check_not_forced.sh gateway/keepalived/notify.sh /usr/local/bin/
COPY gateway/keepalived/keepalived.conf.tmpl gateway/haproxy/bootstrap.cfg gateway/haproxy/maintenance.http /usr/local/share/cstam/
COPY --chmod=755 gateway/entrypoint.sh /usr/local/bin/entrypoint.sh
EXPOSE 80 9000
# tini -g: forward SIGTERM to the whole group so Keepalived can hand the VIP over cleanly on stop.
ENTRYPOINT ["tini", "-g", "--", "/usr/local/bin/entrypoint.sh"]
