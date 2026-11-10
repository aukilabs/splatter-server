# Compute Capability

Expected Input and Output Files from each compute capability

<details>
  <summary>/splatter/colmap/v1</summary>

  DMT Rendering Job typically passes a single `refined_manifest*` URL in `inputs_cids`. The runner expands its `dataIDs` into recordings and downloads matching `colmap_*_bin` artifacts into `refined_sfm_combined`.

  ### Input
  ```bash
  # Input Files
  {job_root_path}
  ├── datasets
  │   └── {scan_id}
  │       └── Frames.mp4
  └── refined
      └── global
          └── refined_sfm_combined
              ├── cameras.bin
              ├── frames.bin
              ├── images.bin
              ├── points3D.bin
              └── rigs.bin
  ```

  ### Output
  ```bash
  # Output Files
  {job_root_path}
  └── refined
      └── splatter
          └── splat_rot.splat    # Uploaded as refined_splat_<suffix> (data_type=splat_data)
  ```

</details>


<details>
  <summary>/splatter/local/v1</summary>

  ### Input
  ```bash
  # Input Files
  {job_root_path}
  └── datasets
      └── {scan_id}
          ├── Frames.mp4
          └── RefinedScan.zip    # COLMAP *.{bin,csv,txt}
  ```

  ### Output
  ```bash
  # Output Files
  {job_root_path}
  └── refined
      └── local
          └── {scan_id}
              └── splat
                  └── splat_{iterations}.filtered.sog    # Uploaded as local_splat_sog_<scan_id>.local_splat_sog
  ```

</details>


<details>
  <summary>/splatter/global/v1</summary>

  ### Input
  ```bash
  # Input Files
  {job_root_path}
  └── refined
      ├── global
      │   └── refined_manifest.json    # from refined_manifest* input
      └── local
          └── {scan_id}
              └── splat
                  └── splat_{iterations}.filtered.sog    # from local_splat_sog_<scan_id> (or via refined_scan_* name resolve)
  ```

  ### Output
  ```bash
  # Output Files
  {job_root_path}
  ├── refined
  │   └── global
  │       ├── coarse_partition_<size>_<x>_<z>.sog
  │       └── fine_partition_<size>_<x>_<z>.sog
  └── output
      └── splat_partition_{coarse|fine}_<size>_<x>_<z>_<manifestTimestamp>.splat_partition_sog
          # Uploaded as data_type=splat_partition_sog; manifestTimestamp from refined_manifest_* name
  ```

</details>
