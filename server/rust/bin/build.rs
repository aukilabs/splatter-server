use std::env;

fn normalize_version(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }

    let normalized = trimmed
        .strip_prefix('v')
        .or_else(|| trimmed.strip_prefix('V'))
        .unwrap_or(trimmed);

    if semver::Version::parse(normalized).is_ok() {
        Some(normalized.to_string())
    } else {
        None
    }
}

fn main() {
    println!("cargo:rerun-if-env-changed=SPLATTER_VERSION");

    let raw = env::var("SPLATTER_VERSION").unwrap_or_else(|_| "0.0.0-local".to_string());
    let normalized = normalize_version(&raw).unwrap_or_else(|| {
        panic!("Invalid SPLATTER_VERSION={raw:?}. Expected semver like 0.3.0 or v0.3.0")
    });

    println!("cargo:rustc-env=SPLATTER_NODE_VERSION={normalized}");
}
