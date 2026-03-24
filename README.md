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
│       ├── splat_rot.splat # this is what needs to be uploaded to dmt
│       ├── preview_top.png    # NEW: top-down preview image
│       ├── preview_angle.png  # NEW: angled 3/4 view preview image
│       ├── preview.mp4        # NEW: orbital preview video
│       └── splatfacto
│           └── {splat torch model}
```
