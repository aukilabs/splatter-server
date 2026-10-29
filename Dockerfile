##
## Multi-stage: rust-build → lichtfeld-build → lean runtime (no /app/vcpkg tree).
## Full image rebuild (rare): `docker build -f Dockerfile -t splatter-server:v0.2-dev .`
## Fast iter: `Dockerfile.r2-overlay` on `splatter-server:v0.2-dev` (pipeline/helper/config/Rust; optional deps via INSTALL_REQUIREMENTS=1).
##
ARG SPLATTER_VERSION=0.0.0-local

##
## Stage 1: Build the Rust compute-node binary (splatter-bin)
##
FROM --platform=$BUILDPLATFORM rust:1.89-bullseye AS rust-build
ARG SPLATTER_VERSION
ENV SPLATTER_VERSION="${SPLATTER_VERSION}"
WORKDIR /app
COPY server/rust/ server/rust/
RUN cargo build --release -p splatter-bin --manifest-path server/rust/Cargo.toml

##
## Stage 2: Build LichtFeld Studio
##
FROM nvidia/cuda:12.8.0-devel-ubuntu24.04 AS lichtfeld-build

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        curl \
        zip \
        unzip \
        tar \
        wget \
        gpg \
        python3 \
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
    && rm -rf /var/lib/apt/lists/*

# LichtFeld needs CMake 3.30+
RUN wget -O - https://apt.kitware.com/keys/kitware-archive-latest.asc 2>/dev/null | gpg --dearmor - | tee /usr/share/keyrings/kitware-archive-keyring.gpg >/dev/null && \
    echo 'deb [signed-by=/usr/share/keyrings/kitware-archive-keyring.gpg] https://apt.kitware.com/ubuntu/ noble main' | tee /etc/apt/sources.list.d/kitware.list >/dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends cmake && \
    rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-14 60 && \
    update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-14 60

WORKDIR /app

RUN git clone https://github.com/microsoft/vcpkg.git
RUN cd vcpkg && ./bootstrap-vcpkg.sh -disableMetrics
# Official LichtFeld release pin for splatter v0.2.
ARG LICHTFELD_GIT_REF=v0.5.4.2
RUN git clone --recursive https://github.com/MrNeRF/LichtFeld-Studio
RUN cd LichtFeld-Studio && git checkout "${LICHTFELD_GIT_REF}" && git submodule update --init --recursive
RUN apt-get update && apt-get install -y --no-install-recommends libgtk-3-dev && rm -rf /var/lib/apt/lists/*
RUN cd LichtFeld-Studio && \
    cmake -B build -DCMAKE_BUILD_TYPE=Release -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE=/app/vcpkg/scripts/buildsystems/vcpkg.cmake \
    -DBUILD_PORTABLE=ON
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1 && \
    ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/lib/x86_64-linux-gnu/libcuda.so.1
RUN (cd LichtFeld-Studio && LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs:${LD_LIBRARY_PATH} cmake --build build -- -j$(nproc))

# Collect the binary, its direct shared-lib deps, and the vcpkg runtime libraries
# (including dlopen'd plugins like nvimgcodec codec extensions) for the runtime stage.
RUN mkdir -p /lichtfeld-dist/lib \
    && cp /app/LichtFeld-Studio/build/LichtFeld-Studio /lichtfeld-dist/ \
    && ldd /app/LichtFeld-Studio/build/LichtFeld-Studio \
       | awk '/=>/{print $3}' \
       | sort -u \
       | while read lib; do \
           [ -f "$lib" ] || continue; \
           case "$lib" in /usr/lib/*|/lib/*) continue ;; esac; \
           cp -L "$lib" /lichtfeld-dist/lib/; \
         done \
    && echo "--- Bundled direct deps ---" && ls -lh /lichtfeld-dist/lib/
# vcpkg runtime libs + plugin subdirs (e.g. nvimgcodec extensions loaded via dlopen)
RUN cp -a /app/LichtFeld-Studio/build/vcpkg_installed/x64-linux/lib/ /lichtfeld-dist/vcpkg_lib/ \
    && echo "--- vcpkg runtime lib tree ---" && find /lichtfeld-dist/vcpkg_lib -name "*.so*" | head -40

##
## Stage 3: Runtime image (lean — no build toolchain, no /app/vcpkg)
##
FROM nvidia/cuda:12.8.0-devel-ubuntu24.04

ARG SPLATTER_VERSION
ENV SPLATTER_SERVER_VERSION="${SPLATTER_VERSION}"

ARG TARGETPLATFORM TARGETARCH TARGETOS
ARG USERNAME=splatter-server
ARG USER_UID=1000
ARG USER_GID=$USER_UID
ARG DEBIAN_FRONTEND=noninteractive

# Runtime-only dependencies (no compilers, no -dev headers)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        python3.12 \
        python3.12-venv \
        python3-pip \
        curl \
        zip \
        unzip \
        tar \
        libglib2.0-0 \
        libxinerama1 \
        libxcursor1 \
        libglu1-mesa \
        libgl1 \
        libxi6 \
        libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

# SOG conversion binary
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g @playcanvas/splat-transform

WORKDIR /app

# Compiled binary + direct shared-lib deps
COPY --from=lichtfeld-build /lichtfeld-dist/LichtFeld-Studio /app/LichtFeld-Studio/build/LichtFeld-Studio
COPY --from=lichtfeld-build /lichtfeld-dist/lib/ /app/LichtFeld-Studio/build/lib/
# vcpkg runtime libs (preserves subdirectory structure for dlopen'd plugins like nvimgcodec)
COPY --from=lichtfeld-build /lichtfeld-dist/vcpkg_lib/ /app/LichtFeld-Studio/build/vcpkg_installed/x64-linux/lib/
ENV LD_LIBRARY_PATH="/app/LichtFeld-Studio/build/lib:/app/LichtFeld-Studio/build/vcpkg_installed/x64-linux/lib:${LD_LIBRARY_PATH}"

# Python environment for the new splatter pipeline
RUN python3.12 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r /app/requirements.txt

# Pipeline code
COPY splatter_pipeline.py artifact_naming.py local_main.py global_main.py combine_splats.py convert_splat.py filter_splats.py preprocessing.py partition_splat.py crop_aabb.py black_pixel_masks.py /app/

COPY scripts/train_helper.sh /app/scripts/
RUN chmod +x /app/scripts/train_helper.sh \
    && sed -i "s/\r$//" /app/scripts/train_helper.sh

COPY config/lichtfeld_optimization_params.json /app/config/

# Compute node binary built from source
COPY --from=rust-build /app/server/rust/target/release/splatter-bin /app/splatter

# Runtime configuration
ENV TASKS_ROOT=/app/tasks
ENV LICHTFELD_BIN=/app/LichtFeld-Studio/build/LichtFeld-Studio
ENV LFS_EXE=/app/LichtFeld-Studio/build/LichtFeld-Studio
ENV LICHTFELD_TRAIN_HELPER=/app/scripts/train_helper.sh
ENV LFS_ROOT=/app/LichtFeld-Studio
ENV LICHTFELD_CONFIG=/app/config/lichtfeld_optimization_params.json
ENV SPLATTER_ENABLE_LICHTFELD_COLMAP=true
ENV SPLATTER_USE_LEGACY_COLMAP_RUN_PY=false

# Non-root runtime: only chown directories the process actually writes to
RUN useradd -u "$USER_UID" -o -m user || true \
    && mkdir -p /app/tasks \
    && chown -R "$USER_UID:$USER_GID" /app/tasks

USER $USER_UID:$USER_GID

EXPOSE 8080
ENTRYPOINT ["/app/splatter"]
