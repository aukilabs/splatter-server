# splatter-server

Splatter compute node for the Auki Network: runs Gaussian splatting jobs (COLMAP + nerfstudio/splatfacto) as part of the reconstruction pipeline.

## License

This project is licensed under the [MIT License](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to report issues, open PRs, and run the Rust checks locally.

## Docker

### Build
From repo root path:
```bash
docker build -t splatter-server .
```

### Run

Create a `.env` from `server/rust/.env.example` and set your DMS/DDS URLs and registration secret, then:

```bash
docker run --gpus all -p 8080:8080 --name splatter -d --env-file .env splatter-server
```

## Run Trainer
```bash
python3 run.py \
--domain_id {domain_id}
--job_id {job_id} \
--job_root_path {path/to/job/root} \
--log_level {log level}
```

## Required Files
```bash
# Input Files
{job_root_path}
├── datasets
│   └── {dataset}
│       └── Frames.mp4
├── refined
│   └── global
│       └── refined_sfm_combined
│           ├── cameras.bin
│           ├── images.bin
│           └── points3D.bin
```
## Output Files
```bash
# Output Files
{job_root_path}
├── Frames
│   ├── {images}
│   └── ...
├── refined
│   ├── nerfstudio-data
│   │   └── {converted nerfstudio data from colmap}
│   └── splatter
│       ├── splat.ply
│       ├── splat_rot.ply
│       ├── splat_rot.splat    # uploaded as "splat_data"
│       ├── preview_top.jpg    # top-down preview, uploaded as "splat_preview_top"
│       ├── preview_angle.jpg  # angled 3/4-view preview, uploaded as "splat_preview_angle"
│       └── splatfacto
│           └── {splat torch model}
```

### Preview Images

After training completes, two preview images are rendered from the trained
Gaussian Splat model (best-effort -- if rendering fails the pipeline still
succeeds):

| File | View | Description |
|------|------|-------------|
| `preview_top.jpg` | Top-down | Camera directly above the centroid looking straight down. Shows the spatial footprint / floor-plan layout. |
| `preview_angle.jpg` | Angled 3/4 | Camera at an elevated corner (~45 deg) looking at the centroid. Shows depth and vertical structure. |

Both previews are uploaded to the domain alongside the `.splat` file so
downstream services can quickly assess training quality without loading the
full splat.
