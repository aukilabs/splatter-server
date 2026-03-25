# Minimum Requirements

The splatter node has fairly high requirements as the training of a realistic gaussian splat is computationally intensive. Please ensure your system is able to handle high CPU and GPU load for extended periods without overheating.

**NOTE:** The requirements below are high to ensure smooth processing. If you successfully run on lower hardware specs, please let us know!

- **OS (64-bit):** Windows 10 / 11 or Ubuntu 22.04 / 24.04 LTS
- **CPU:** 4 cores
- **RAM:** 12 GiB (recommended 16 GiB or more)
- **GPU:** Nvidia with 8+ GiB VRAM. Tested on RTX 3090, RTX 4060 and T4. RTX 50xx is not currently supported, but planned for upcoming releases. May work on older Nvidia cards too with enough VRAM and recent CUDA.
- **NVIDIA driver:** Recent driver with CUDA 11.8 support.
- **Disk space:** 40 GB or more
- **Docker** _- Windows support tested with Docker Desktop and WSL 2_
- Sufficient cooling and power supply to handle high load.
- A stable Internet connection with at least 10 Mbps downstream and upstream

See [Deployment](deployment.md) for more information.