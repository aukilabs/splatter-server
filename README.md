# Splatter Node
This repository contains the splatter node, part of the Auki Network. This node enables photorealistic scene rendering by training 3D Gaussian Splats.

The splatter node operates in conjunction with the [reconstruction node](https://github.com/aukilabs/reconstruction-server) and the scans from the Domain Management Tool (DMT) app ([App Store](https://apps.apple.com/app/domain-management-tool/id6499270503) 🔗). The refined camera poses from the reconstruction node are used as a starting point for training the gaussian splat, making it more robust to challenging indoor environments and noisy captures.

For more information about the reconstruction and rendering pipeline, please refer to our [whitepaper](https://auki.gitbook.io/whitepaper/technical-overview/the-reconstruction-service).

## Documentation
- [Minimum Requirements](docs/minimum-requirements.md)
- [Deployment](docs/deployment.md)
- [Contributing](CONTRIBUTING.md)

## License

This project is licensed under the [MIT License](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to report issues, open PRs, and run the Rust checks locally.

## Acknowledgments

This project builds upon the work of many excellent open-source projects, including
[nerfstudio](https://github.com/nerfstudio-project/nerfstudio), [ply2splat](https://github.com/bastikohn/ply2splat), [PyTorch](https://pytorch.org),
[OpenCV](https://opencv.org), [Open3D](https://www.open3d.org), and others.

We thank their authors and contributors for making this work possible.  
Please note that all third-party code and libraries are subject to their respective licenses, copyrights, and trademarks.
We are not affiliated with, endorsed by, or sponsored by any of the projects or organizations mentioned above.