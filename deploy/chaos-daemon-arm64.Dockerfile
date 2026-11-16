FROM rust:1.99.1-bookworm AS build
ARG TARGETARCH
WORKDIR /toda-build
COPY . .
ENV RUSTFLAGS="-C relro-level=full"
RUN --mount=type=cache,id=toda-target-1.99.1-bookworm-${TARGETARCH},target=/toda-build/target,sharing=locked \
    --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    cargo build --locked --release && cp target/release/toda /toda
ADD --checksum=sha256:c91e44c3e6b0f2b9a2204b31403f0f1bb9a33d5bb0446a8fdf8b44aeba534e70 https://github.com/chaos-mesh/chaos-tproxy/releases/download/v0.6.0/tproxy-aarch64.tar.gz /tmp/chaos-tproxy.tar.gz
RUN tar -xzf /tmp/chaos-tproxy.tar.gz -C /tmp ./tproxy

FROM ghcr.io/chaos-mesh/chaos-daemon:v2.8.8
COPY --from=build /toda /tmp/tproxy /usr/local/bin/
