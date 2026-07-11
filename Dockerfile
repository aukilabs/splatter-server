##
## Build the Rust compute-node binary (splatter-bin)
##
ARG SPLATTER_VERSION=0.0.0-local
FROM --platform=$BUILDPLATFORM rust:1.89-bullseye AS rust-build
ARG SPLATTER_VERSION
ENV SPLATTER_VERSION="${SPLATTER_VERSION}"
WORKDIR /app
COPY server/rust/ server/rust/
RUN cargo build --release -p splatter-bin --manifest-path server/rust/Cargo.toml

##
## Runtime image with nerfstudio + splatter runner
##
FROM ghcr.io/nerfstudio-project/nerfstudio:latest

ARG SPLATTER_VERSION
ENV SPLATTER_SERVER_VERSION="${SPLATTER_VERSION}"

ARG USERNAME=splatter-server
ARG USER_UID=1000
ARG USER_GID=$USER_UID
ARG DEBIAN_FRONTEND=noninteractive
ENV TASKS_ROOT=/app/tasks

# Keep the original Python dependency footprint
RUN python3 -m pip install --no-cache-dir ply2splat git+https://github.com/francescofugazzi/3dgsconverter.git@0.8

WORKDIR /app

# Job pipeline scripts (run.py drives ns-process-data / ns-train)
COPY run.py extract_mp4.py convert_ply2splat.py rotate_ply.py compress_sog.py /app/

# Compute node binary built from source
COPY --from=rust-build /app/server/rust/target/release/splatter-bin /app/compute-node

# Non-root user and writable workspace for task payloads
RUN groupadd --gid "$USER_GID" "$USERNAME" \
    && useradd --uid "$USER_UID" --gid "$USER_GID" -m "$USERNAME" \
    && mkdir -p /app/tasks \
    && chown -R "$USERNAME:$USERNAME" /app

USER $USERNAME

EXPOSE 8080
ENTRYPOINT ["/app/compute-node"]
