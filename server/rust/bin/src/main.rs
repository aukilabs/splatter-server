use anyhow::Result;
use posemesh_compute_node::engine::RunnerRegistry;
use posemesh_compute_node::telemetry;
use std::env;
use std::path::PathBuf;
use tracing::info;
use tracing::warn;

const SPLATTER_NODE_VERSION: &str = env!("SPLATTER_NODE_VERSION");

fn tasks_cleanup_disabled() -> bool {
    match env::var("DISABLE_TASKS_CLEANUP") {
        Ok(v) => matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"),
        Err(_) => false,
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    // Load .env from CWD and crate dir for convenience.
    let _ = dotenvy::from_filename(".env");
    let _ = dotenvy::from_path(concat!(env!("CARGO_MANIFEST_DIR"), "/.env"));

    telemetry::init_from_env()?;

    // Best-effort cleanup of any stale task workspaces from previous runs.
    let task_root = env::var("TASKS_ROOT").unwrap_or_else(|_| "tasks".to_string());
    let task_root_path = PathBuf::from(&task_root);
    if let Err(err) = tokio::fs::create_dir_all(&task_root_path).await {
        warn!(%err, path = %task_root_path.display(), "failed to ensure TASKS_ROOT exists");
    } else if !tasks_cleanup_disabled() {
        if let Ok(mut rd) = tokio::fs::read_dir(&task_root_path).await {
            while let Ok(Some(entry)) = rd.next_entry().await {
                let path = entry.path();
                if let Ok(ft) = entry.file_type().await {
                    if !ft.is_dir() {
                        continue;
                    }
                }
                if let Err(err) = tokio::fs::remove_dir_all(&path).await {
                    warn!(%err, path = %path.display(), "failed to remove stale task workspace");
                }
            }
        }
    } else {
        info!(
            path = %task_root_path.display(),
            "startup task cleanup disabled; leaving existing workspaces"
        );
    }

    let app = posemesh_compute_node::http::router();
    let listener = tokio::net::TcpListener::bind("0.0.0.0:8080").await?;
    let addr = listener.local_addr()?;
    println!("http listening on {}", addr);
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });

    let mut cfg = posemesh_compute_node::config::NodeConfig::from_env()?;
    cfg.node_version = SPLATTER_NODE_VERSION.to_string();

    let registry: RunnerRegistry = splatter_runner::registry();
    let capabilities = registry.capabilities();

    posemesh_compute_node::dds::register::spawn_registration_if_configured(&cfg, &capabilities)?;
    info!(?capabilities, "splatter runner registered capabilities");

    posemesh_compute_node::engine::run_node(cfg, registry).await?;

    Ok(())
}
