import re


def _lod_from_filename(filename: str) -> str | None:
    if filename.startswith("coarse_partition_"):
        return "coarse"
    if filename.startswith("fine_partition_"):
        return "fine"
    return None


def rename_for_domain_upload(
    filename: str,
    manifest_timestamp: str | None = None,
    lod_level: str | None = None,
) -> str | None:
    """Map generated global partition outputs to domain upload naming."""
    if filename in {"combined_splat.ply", "coarse.ply", "fine.ply"}:
        return None

    lod = lod_level or _lod_from_filename(filename)
    if lod is None:
        return None

    splat_match = re.match(rf"{lod}_partition_(.+)\.splat$", filename)
    if splat_match:
        suffix = splat_match.group(1)
        if manifest_timestamp:
            return (
                f"splat_partition_{lod}_{suffix}_{manifest_timestamp}"
                f".splat_partition"
            )
        return f"splat_partition_{suffix}.splat_partition"

    sog_match = re.match(rf"{lod}_partition_(.+)\.sog$", filename)
    if sog_match:
        if not manifest_timestamp:
            return None
        suffix = sog_match.group(1)
        return (
            f"splat_partition_{lod}_{suffix}_{manifest_timestamp}"
            f".splat_partition_sog"
        )

    return None


def colmap_refined_splat_upload_name(refined_suffix: str | None) -> str:
    """Domain artifact name for ``/splatter/colmap/v1`` (mirrors Rust upload_colmap_result)."""
    if not refined_suffix:
        return "refined_splat"
    if refined_suffix.startswith("_"):
        return f"refined_splat{refined_suffix}"
    return f"refined_splat_{refined_suffix}"
