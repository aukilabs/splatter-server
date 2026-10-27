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
## Runtime image
##
FROM nvidia/cuda:12.8.0-devel-ubuntu24.04

ARG SPLATTER_VERSION
ENV SPLATTER_SERVER_VERSION="${SPLATTER_VERSION}"

ARG TARGETPLATFORM TARGETARCH TARGETOS
ARG USERNAME=splatter-server
ARG USER_UID=1000
ARG USER_GID=$USER_UID
ARG DEBIAN_FRONTEND=noninteractive

# Python + build/runtime dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        python3.12 \
        python3.12-venv \
        python3-pip \
        git \
        curl \
        zip \
        unzip \
        gpg \
        wget \
        tar \
        gcc-14 \
        g++-14 \
        gfortran-14 \
        autoconf \
        autoconf-archive \
        automake \
        libtool \
        linux-libc-dev \
        nasm \
        yasm \
        ninja-build \
        pkg-config \
        libxinerama-dev \
        libxcursor-dev \
        xorg-dev \
        libglu1-mesa-dev \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# LichtFeld needs CMake 3.30+
RUN wget -O - https://apt.kitware.com/keys/kitware-archive-latest.asc 2>/dev/null | gpg --dearmor - | tee /usr/share/keyrings/kitware-archive-keyring.gpg >/dev/null && \
    echo 'deb [signed-by=/usr/share/keyrings/kitware-archive-keyring.gpg] https://apt.kitware.com/ubuntu/ noble main' | tee /etc/apt/sources.list.d/kitware.list >/dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends cmake && \
    rm -rf /var/lib/apt/lists/*

# SOG conversion binary
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g @playcanvas/splat-transform

# For LichtFeld Studio we need GCC/G++ 14
RUN update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-14 60 && \
    update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-14 60

WORKDIR /app

# Build LichtFeld Studio (official release pin for splatter v0.2).
ARG LICHTFELD_GIT_REF=v0.5.4.2
RUN git clone https://github.com/microsoft/vcpkg.git
RUN cd vcpkg && ./bootstrap-vcpkg.sh -disableMetrics
RUN git clone --recursive https://github.com/MrNeRF/LichtFeld-Studio
RUN cd LichtFeld-Studio && git checkout "${LICHTFELD_GIT_REF}" && git submodule update --init --recursive
# Latest LichtFeld NFD GTK backend needs libgtk-3-dev at configure time
RUN apt-get update && apt-get install -y --no-install-recommends libgtk-3-dev && rm -rf /var/lib/apt/lists/*
RUN cd LichtFeld-Studio && \
    cmake -B build -DCMAKE_BUILD_TYPE=Release -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE=/app/vcpkg/scripts/buildsystems/vcpkg.cmake \
    -DBUILD_PORTABLE=ON
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1 && \
    ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/lib/x86_64-linux-gnu/libcuda.so.1
RUN (cd LichtFeld-Studio && LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs:${LD_LIBRARY_PATH} cmake --build build -- -j$(nproc))

# Python environment for the new splatter pipeline
RUN python3.12 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r /app/requirements.txt

# Pipeline code
COPY splatter_pipeline.py artifact_naming.py local_main.py global_main.py combine_splats.py convert_splat.py filter_splats.py preprocessing.py partition_splat.py crop_aabb.py black_pixel_masks.py densify_init.py /app/

# Train helper (vendored; recipe in config JSON)
COPY scripts/train_helper.sh scripts/run_densify_cli.py /app/scripts/
RUN chmod +x /app/scripts/train_helper.sh /app/scripts/run_densify_cli.py \
    && sed -i "s/\r$//" /app/scripts/train_helper.sh /app/scripts/run_densify_cli.py

# --- RoMa densification plugin (S5) ---
# Skipped for now (2026-10-25): bake is flaky / slow; product runs without densify.
# Re-enable: restore densify-venv clone + deps install + romav2.pt, then SPLATTER_DENSIFY=1.
ENV DENSIFY_PLUGIN_ROOT=/app/Lichtfeld-Densification-Plugin
ENV DENSIFY_CLI_SCRIPT=/app/scripts/run_densify_cli.py
ENV DENSIFY_ROMA_SETTING=turbo
ENV DENSIFY_NUM_REFS=0.6
ENV SPLATTER_DENSIFY=0

# LichtFeld train config (lichtfeld_optimization_params.json)
COPY config/lichtfeld_optimization_params.json config/lichtfeld_optimization_params_vda.json /app/config/

# Legacy scripts for fallback mode if explicitly enabled
COPY run.py extract_mp4.py convert_ply2splat.py rotate_ply.py /app/

# Compute node binary built from source
COPY --from=rust-build /app/server/rust/target/release/splatter-bin /app/splatter

# Runtime configuration
ENV TASKS_ROOT=/app/tasks
ENV LICHTFELD_BIN=/app/LichtFeld-Studio/build/LichtFeld-Studio
ENV LFS_EXE=/app/LichtFeld-Studio/build/LichtFeld-Studio
ENV LICHTFELD_TRAIN_HELPER=/app/scripts/train_helper.sh
ENV LFS_ROOT=/app/LichtFeld-Studio
ENV LICHTFELD_CONFIG=/app/config/lichtfeld_optimization_params.json
ENV LICHTFELD_CONFIG_VDA=/app/config/lichtfeld_optimization_params_vda.json

# Install sudo for runtime elevated permissions if needed
RUN apt-get update && apt-get install -y sudo && rm -rf /var/lib/apt/lists/*

# Non-root runtime: only chown directories the process actually writes to.
# Densify paths omitted while SPLATTER_DENSIFY=0 / bake skipped.
RUN mkdir -p /app/tasks \
    && chown -R "$USER_UID:$USER_GID" /app/tasks

# Add $USER_UID as a user with sudo privileges (without password)
RUN useradd -u "$USER_UID" -o -m user || true \
    && echo "user ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

USER $USER_UID:$USER_GID

EXPOSE 8080
ENTRYPOINT ["/app/splatter"]
