import re


def rename_for_domain_upload(
    filename: str,
    manifest_timestamp: str | None = None,
    lod_level: str = "full",
) -> str | None:
    """Map generated global partition outputs to domain upload naming."""
    if filename == "combined_splat.ply":
        return None

    splat_match = re.match(r"combined_splat_partition_(.+)\.splat$", filename)
    if splat_match:
        suffix = splat_match.group(1)
        if manifest_timestamp:
            return (
                f"splat_partition_{lod_level}_{suffix}_{manifest_timestamp}"
                f".splat_partition"
            )
        return f"splat_partition_{suffix}.splat_partition"

    sog_match = re.match(r"combined_splat_partition_(.+)\.sog$", filename)
    if sog_match:
        if not manifest_timestamp:
            return None
        suffix = sog_match.group(1)
        return (
            f"splat_partition_{lod_level}_{suffix}_{manifest_timestamp}"
            f".splat_partition_sog"
        )

    return None
