# Splatter Node
This repository contains the splatter node, part of the Auki Network. This node enables photorealistic scene rendering by training 3D Gaussian Splats.

The splatter node operates in conjunction with the [reconstruction node](https://github.com/aukilabs/reconstruction-server) and the scans from the Domain Management Tool (DMT) app ([App Store](https://apps.apple.com/app/domain-management-tool/id6499270503) 🔗). The refined camera poses from the reconstruction node are used as a starting point for training the gaussian splat, making it more robust to challenging indoor environments and noisy captures.

For more information about the reconstruction and rendering pipeline, please refer to our [whitepaper](https://auki.gitbook.io/whitepaper/technical-overview/the-reconstruction-service).

## Documentation
- [Minimum Requirements](docs/minimum-requirements.md)
- [Deployment](docs/deployment.md)
- [Contributing](CONTRIBUTING.md)

## Output Files

After training, the node exports the splat and attempts to render two images and
a five-second orbital video from the trained model. Preview files are written
under `{job_root_path}/refined/splatter/`:

| File | View | Domain data type | Uploaded name |
| --- | --- | --- | --- |
| `splat_rot.splat` | Trained scene | `splat_data` | `refined_splat{suffix}` |
| `preview_top.jpg` | Top-down view | `splat_preview_top` | `refined_splat_preview_top{suffix}` |
| `preview_angle.jpg` | Elevated 45-degree corner view | `splat_preview_angle` | `refined_splat_preview_angle{suffix}` |
| `preview.mp4` | 360-degree orbit at 35-degree elevation | `splat_preview_video` | `refined_splat_preview_video{suffix}` |

The names use the refined manifest's existing suffix, adding a separating
underscore when needed. Without a suffix, the base names are used. The MP4 uses
H.264 at 30 fps, with browser-compatible pixel format and fast-start metadata.
Temporary rendered video frames are removed after encoding.

The final progress event lists the names of previews that were successfully
uploaded, for example:

```json
{
  "progress": 100,
  "stage": "complete",
  "status": "succeeded",
  "preview_artifacts": [
    "refined_splat_preview_top_2026-09-08_12-34-56",
    "refined_splat_preview_angle_2026-09-08_12-34-56",
    "refined_splat_preview_video_2026-09-08_12-34-56"
  ]
}
```

Previews are optional: rendering or uploading a preview logs a warning on
failure and allows the main splat job to complete. Failed or skipped previews
are omitted from `preview_artifacts`; the list is empty if none upload. The
Docker runtime includes the PLY reader and FFmpeg required by this step.

## License

This project is licensed under the [MIT License](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to report issues, open PRs, and run the Rust checks locally.

## Acknowledgments

This project builds upon the work of many excellent open-source projects, including
[nerfstudio](https://github.com/nerfstudio-project/nerfstudio), [ply2splat](https://github.com/bastikohn/ply2splat), [PyTorch](https://pytorch.org),
[OpenCV](https://opencv.org), [Open3D](https://www.open3d.org), and others.

Preview generation and artifact upload build on
[Justin Lee Yang's contribution](https://github.com/aukilabs/splatter-server/pull/16).

We thank their authors and contributors for making this work possible.  
Please note that all third-party code and libraries are subject to their respective licenses, copyrights, and trademarks.
We are not affiliated with, endorsed by, or sponsored by any of the projects or organizations mentioned above.
