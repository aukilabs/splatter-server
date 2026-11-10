use anyhow::{anyhow, Context, Result};
use async_trait::async_trait;
use node_host::{
    process, telemetry, ArtifactContent, ArtifactRequest, Materialized, NodeRunner, Router,
    TaskContext, TaskIo,
};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet, VecDeque};
use std::env;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Instant;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;
use tracing::{info, warn};
use uuid::Uuid;

pub const CAPABILITY_COLMAP_V1: &str = "/splatter/colmap/v1";
pub const CAPABILITY_LOCAL_V1: &str = "/splatter/local/v1";
pub const CAPABILITY_GLOBAL_V1: &str = "/splatter/global/v1";

pub fn registry() -> Router {
    Router::new()
        .register(SplatterRunner::new(CAPABILITY_COLMAP_V1))
        .register(SplatterRunner::new(CAPABILITY_LOCAL_V1))
        .register(SplatterRunner::new(CAPABILITY_GLOBAL_V1))
}

#[derive(Clone, Copy)]
pub struct SplatterRunner {
    capability: &'static str,
}

impl SplatterRunner {
    pub const fn new(capability: &'static str) -> Self {
        Self { capability }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CapabilityMode {
    ColmapV1,
    LocalV1,
    GlobalV1,
}

#[derive(Default, Debug)]
struct DownloadSummary {
    scan_ids: Vec<String>,
    datasets_downloaded: usize,
}

#[derive(Clone, Debug)]
struct DataMeta {
    id: Uuid,
    size: u64,
    name: String,
    data_type: String,
}

/// Structured progress parsed from a Python `[PROGRESS]` line.
struct PipelineProgress {
    stage: String,
    pct: Option<u8>,
    detail: String,
}

/// Try to parse `[PROGRESS] stage=... pct=... detail=...` from a stdout line.
fn parse_progress_line(line: &str) -> Option<PipelineProgress> {
    let rest = line.strip_prefix("[PROGRESS] ")?;
    let mut stage = String::new();
    let mut pct: Option<u8> = None;
    let mut detail = String::new();
    for token in rest.split_whitespace() {
        if let Some(v) = token.strip_prefix("stage=") {
            stage = v.to_string();
        } else if let Some(v) = token.strip_prefix("pct=") {
            pct = v.parse().ok();
        } else if let Some(v) = token.strip_prefix("detail=") {
            detail = v.to_string();
        } else if !detail.is_empty() {
            detail.push(' ');
            detail.push_str(token);
        }
    }
    if stage.is_empty() {
        return None;
    }
    Some(PipelineProgress { stage, pct, detail })
}

/// Try to extract training iteration progress from lines like:
/// `Training [...] 45% [01m:30s<01m:50s] 9000/30000 | Loss: 0.0501`
fn parse_training_progress(line: &str) -> Option<(u32, u32, f32)> {
    if !line.contains("Training") || !line.contains('/') {
        return None;
    }
    // Find the iter/total pattern: <current>/<total>
    let mut iter_cur = None;
    let mut iter_total = None;
    let mut loss = None;
    for token in line.split_whitespace() {
        if token.contains('/') && iter_cur.is_none() {
            let mut parts = token.split('/');
            if let (Some(c), Some(t)) = (parts.next(), parts.next()) {
                if let (Ok(c), Ok(t)) = (c.parse::<u32>(), t.parse::<u32>()) {
                    iter_cur = Some(c);
                    iter_total = Some(t);
                }
            }
        }
        if let Some(v) = token.strip_prefix("Loss:") {
            loss = v.trim().parse::<f32>().ok();
        }
    }
    // Also try token after "Loss:"
    if loss.is_none() {
        let parts: Vec<&str> = line.split("Loss:").collect();
        if parts.len() > 1 {
            loss = parts[1].trim().split_whitespace().next()
                .and_then(|v| v.parse::<f32>().ok());
        }
    }
    match (iter_cur, iter_total) {
        (Some(c), Some(t)) => Some((c, t, loss.unwrap_or(0.0))),
        _ => None,
    }
}

fn tasks_cleanup_disabled() -> bool {
    match env::var("DISABLE_TASKS_CLEANUP") {
        Ok(v) => matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"),
        Err(_) => false,
    }
}

fn bool_env(name: &str, default: bool) -> bool {
    match env::var(name) {
        Ok(v) => matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"),
        Err(_) => default,
    }
}

fn splatter_iterations() -> String {
    env::var("SPLATTER_ITERATIONS").unwrap_or_else(|_| "30000".to_string())
}

fn splatter_partition_size() -> String {
    env::var("SPLATTER_PARTITION_SIZE").unwrap_or_else(|_| "10".to_string())
}

/// Map a bare `inputs_cids` artifact name to (lookup_name, data_type) for `io.find`.
fn input_name_query(mode: CapabilityMode, raw: &str) -> Option<(String, &'static str)> {
    let raw = raw.trim();
    if raw.starts_with("dmt_recording_") {
        let query_name = raw.trim_end_matches(".mp4").to_string();
        return Some((query_name, "dmt_recording_mp4"));
    }
    if let Some(scan_id) = raw.strip_prefix("refined_scan_") {
        if scan_id.is_empty() {
            return None;
        }
        return match mode {
            CapabilityMode::GlobalV1 => Some((format!("local_splat_sog_{scan_id}"), "local_splat_sog")),
            CapabilityMode::ColmapV1 | CapabilityMode::LocalV1 => {
                Some((raw.to_string(), "refined_scan_zip"))
            }
        };
    }
    if raw.starts_with("refined_manifest")
        && matches!(mode, CapabilityMode::ColmapV1 | CapabilityMode::GlobalV1)
    {
        return Some((raw.to_string(), "refined_manifest_json"));
    }
    if mode == CapabilityMode::GlobalV1 && raw.starts_with("local_splat_sog_") {
        return Some((raw.to_string(), "local_splat_sog"));
    }
    None
}

fn is_refined_manifest_input(name: Option<&str>, data_type: Option<&str>) -> bool {
    data_type == Some("refined_manifest_json")
        || name.is_some_and(|n| n.starts_with("refined_manifest"))
}

/// Reject bare `dataIDs` job envelopes. Refined manifests also carry `dataIDs` (DMT
/// `/splatter/colmap/v1` sends the manifest URL); those are accepted and expanded.
fn reject_non_manifest_data_ids_envelope(
    bytes: &[u8],
    cid: &str,
    name: Option<&str>,
    data_type: Option<&str>,
) -> Result<()> {
    let Ok(parsed) = serde_json::from_slice::<Value>(bytes) else {
        return Ok(());
    };
    if parsed.get("dataIDs").is_none() {
        return Ok(());
    }
    if is_refined_manifest_input(name, data_type) {
        return Ok(());
    }
    Err(anyhow!(
        "input {} uses a dataIDs JSON envelope; pass a refined_manifest URL/name (colmap) or domain-data URLs / artifact names in inputs_cids",
        cid
    ))
}

fn scan_id_from_manifest_data_id_entry(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed.contains("://") || Uuid::parse_str(trimmed).is_ok() {
        return None;
    }
    if let Some(rest) = trimmed.strip_prefix("refined_scan_") {
        return Some(normalize_scan_id(rest));
    }
    if let Some(rest) = trimmed.strip_prefix("dmt_recording_") {
        return Some(normalize_scan_id(rest.trim_end_matches(".mp4")));
    }
    Some(normalize_scan_id(trimmed))
}

fn scan_ids_from_manifest_data_ids(parsed: &Value) -> Vec<String> {
    let Some(arr) = parsed.get("dataIDs").and_then(|v| v.as_array()) else {
        return Vec::new();
    };
    let mut ids = HashSet::new();
    for entry in arr {
        let Some(raw) = entry.as_str() else {
            continue;
        };
        if let Some(scan_id) = scan_id_from_manifest_data_id_entry(raw) {
            ids.insert(scan_id);
        }
    }
    let mut out: Vec<String> = ids.into_iter().collect();
    out.sort();
    out
}

async fn download_colmap_recordings_for_scans(
    scan_ids: &[String],
    job_root: &Path,
    task: &TaskContext,
    io: &TaskIo,
) -> Result<DownloadSummary> {
    let mut summary = DownloadSummary::default();
    let mut downloaded: HashSet<String> = HashSet::new();

    for scan_id in scan_ids {
        if !downloaded.insert(scan_id.clone()) {
            continue;
        }
        ensure_task_not_cancelled(task, "resolving derived recording metadata")?;
        let query_name = format!("dmt_recording_{scan_id}");
        match io.find(&query_name, Some("dmt_recording_mp4")).await {
            Ok(meta_list) => {
                let Some(meta) = meta_list.into_iter().next() else {
                    warn!(
                        scan_id = %scan_id,
                        name = %query_name,
                        "derived dmt recording metadata empty"
                    );
                    continue;
                };
                let dest = job_root
                    .join("datasets")
                    .join(scan_id)
                    .join("Frames.mp4");
                let bytes = download_item(io, meta.id, meta.size, &dest).await?;
                summary.datasets_downloaded += 1;
                summary.scan_ids.push(scan_id.clone());
                let _ = task.log_event(json!({
                    "level": "info",
                    "message": "downloaded derived dmt recording",
                    "data_id": meta.id.to_string(),
                    "scan_id": scan_id,
                    "bytes": bytes,
                    "dest": dest.display().to_string(),
                }));
            }
            Err(err) => {
                warn!(
                    scan_id = %scan_id,
                    name = %query_name,
                    error = %err,
                    "failed to resolve derived dmt recording metadata"
                );
            }
        }
    }

    Ok(summary)
}

async fn expand_colmap_inputs_from_manifest_bytes(
    bytes: &[u8],
    job_root: &Path,
    task: &TaskContext,
    io: &TaskIo,
) -> Result<DownloadSummary> {
    let parsed: Value = serde_json::from_slice(bytes)
        .context("parse refined_manifest JSON for colmap dataIDs")?;
    let scan_ids = scan_ids_from_manifest_data_ids(&parsed);
    if scan_ids.is_empty() {
        warn!("refined_manifest has no usable dataIDs entries for colmap recordings");
        return Ok(DownloadSummary::default());
    }
    download_colmap_recordings_for_scans(&scan_ids, job_root, task, io).await
}

fn merge_download_summary(into: &mut DownloadSummary, from: DownloadSummary) {
    into.datasets_downloaded += from.datasets_downloaded;
    for scan_id in from.scan_ids {
        if !into.scan_ids.contains(&scan_id) {
            into.scan_ids.push(scan_id);
        }
    }
}

async fn stage_materialized_url_input(
    mode: CapabilityMode,
    materialized: &Materialized,
    job_root: &Path,
    task: &TaskContext,
    io: &TaskIo,
) -> Result<DownloadSummary> {
    let mut summary = DownloadSummary::default();
    let name = materialized.name.as_deref();
    let data_type = materialized.data_type.as_deref();
    let cid = materialized.cid.as_str();

    let bytes = tokio::fs::read(&materialized.path)
        .await
        .with_context(|| format!("read materialized path {}", materialized.path.display()))?;
    reject_non_manifest_data_ids_envelope(&bytes, cid, name, data_type)?;

    let is_refined_manifest = is_refined_manifest_input(name, data_type);

    // DMT Rendering Job: inputs_cids = [refined_manifest URL]. Expand dataIDs → recordings.
    if mode == CapabilityMode::ColmapV1 && is_refined_manifest {
        info!(name = ?name, "expanding colmap inputs from refined_manifest url");
        let _ = task.log_event(json!({
            "level": "info",
            "message": "expanding colmap inputs from refined_manifest url",
            "name": name,
            "cid": cid,
        }));
        return expand_colmap_inputs_from_manifest_bytes(&bytes, job_root, task, io).await;
    }

    if mode == CapabilityMode::GlobalV1 {
        if is_refined_manifest {
            let dest = job_root
                .join("refined")
                .join("global")
                .join("refined_manifest.json");
            if let Some(parent) = dest.parent() {
                tokio::fs::create_dir_all(parent).await?;
            }
            tokio::fs::write(&dest, &bytes).await.with_context(|| {
                format!("stage refined manifest to {}", dest.display())
            })?;
            info!(dest = %dest.display(), name = ?name, "staged refined manifest from url input");
            let _ = task.log_event(json!({
                "level": "info",
                "message": "staged refined manifest from url input",
                "dest": dest.display().to_string(),
                "name": name,
            }));
            return Ok(summary);
        }

        if data_type == Some("local_splat_sog") || name.is_some_and(|n| n.starts_with("local_splat_sog_"))
        {
            let name = name.ok_or_else(|| {
                anyhow!("local_splat_sog url input missing artifact name (cid={})", cid)
            })?;
            let scan_id = normalize_scan_id(name.trim_start_matches("local_splat_sog_"));
            let sog_name = format!("splat_{}.filtered.sog", splatter_iterations());
            let dest = job_root
                .join("refined")
                .join("local")
                .join(&scan_id)
                .join("splat")
                .join(sog_name);
            if let Some(parent) = dest.parent() {
                tokio::fs::create_dir_all(parent).await?;
            }
            tokio::fs::copy(&materialized.path, &dest).await?;
            tokio::fs::create_dir_all(job_root.join("datasets").join(&scan_id)).await?;
            summary.datasets_downloaded += 1;
            summary.scan_ids.push(scan_id);
            return Ok(summary);
        }
    }

    if mode != CapabilityMode::GlobalV1 && is_direct_dataset_input(name, data_type) {
        let scan_id = if mode == CapabilityMode::LocalV1 {
            scan_id_from_dataset_artifact(name, data_type).ok_or_else(|| {
                anyhow!(
                    "local capability requires scan_id from artifact name/data_type (cid={})",
                    cid
                )
            })?
        } else {
            scan_id_from_dataset_artifact(name, data_type).unwrap_or_else(|| {
                materialized
                    .path
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .map(normalize_scan_id)
                    .unwrap_or_else(|| "scan".to_string())
            })
        };
        let Some(dest) = dataset_dest_path(job_root, &scan_id, name, data_type) else {
            return Err(anyhow!("cannot stage dataset url input {}", cid));
        };
        if let Some(parent) = dest.parent() {
            tokio::fs::create_dir_all(parent).await?;
        }
        tokio::fs::copy(&materialized.path, &dest).await?;
        summary.datasets_downloaded += 1;
        summary.scan_ids.push(scan_id);
        return Ok(summary);
    }

    warn!(
        cid = %cid,
        name = ?name,
        data_type = ?data_type,
        "url input did not match a known staging rule"
    );
    Ok(summary)
}

async fn download_item(io: &TaskIo, id: Uuid, size: u64, dest: &Path) -> Result<u64> {
    let result = io.download_to(id, size, dest).await;
    if result.is_err() {
        let _ = tokio::fs::remove_file(dest).await;
        if let Some(parent) = dest.parent() {
            let _ = tokio::fs::remove_dir(parent).await;
        }
    }
    result
}

const TASK_CANCELLED_PREFIX: &str = "task cancelled";

fn ensure_task_not_cancelled(task: &TaskContext, stage: &str) -> Result<()> {
    if task.is_cancelled() {
        return Err(anyhow!("{TASK_CANCELLED_PREFIX}: {stage}"));
    }
    Ok(())
}

fn is_task_cancelled_error(err: &anyhow::Error) -> bool {
    err.chain()
        .any(|cause| cause.to_string().contains(TASK_CANCELLED_PREFIX))
}

fn is_url_cid(cid: &str) -> bool {
    cid.contains("://")
}

#[async_trait]
impl NodeRunner for SplatterRunner {
    fn capability(&self) -> &'static str {
        self.capability
    }

    async fn run(&self, task: &TaskContext, io: &TaskIo) -> Result<()> {
        let spec = &task.task;
        let domain_id = io.domain_id();

        let task_span = telemetry::task_span(
            spec.id,
            spec.job_id.unwrap_or_else(Uuid::nil),
            &spec.capability,
            domain_id,
        );
        let _span_guard = task_span.enter();

        ensure_task_not_cancelled(task, "before execution")?;
        let mut refined_suffix: Option<String> = None;
        let mode = match self.capability {
            CAPABILITY_COLMAP_V1 => CapabilityMode::ColmapV1,
            CAPABILITY_LOCAL_V1 => CapabilityMode::LocalV1,
            CAPABILITY_GLOBAL_V1 => CapabilityMode::GlobalV1,
            _ => return Err(anyhow!("unsupported capability {}", self.capability)),
        };
        // Resolve task workspace root; default is relative "tasks" for local dev,
        // but Docker image sets TASKS_ROOT=/app/tasks to avoid cwd/permission issues.
        let task_root = env::var("TASKS_ROOT").unwrap_or_else(|_| "tasks".to_string());
        let job_root = PathBuf::from(task_root).join(spec.id.to_string());
        tokio::fs::create_dir_all(&job_root)
            .await
            .with_context(|| format!("create job root {}", job_root.display()))?;
        let datasets_dir = job_root.join("datasets");

        let task_result: Result<()> = async {
            task.progress(json!({ "status": "started" }))?;

            let input_cids = spec.inputs_cids.clone();
            info!(?input_cids, "input cids");
            let mut summary = DownloadSummary::default();

            if input_cids.is_empty() {
                return Err(anyhow!("no input cid provided; cannot run splatter job"));
            }

            for cid in input_cids {
                if !is_url_cid(&cid) {
                    let Some((query_name, data_type)) = input_name_query(mode, &cid) else {
                        return Err(anyhow!("unsupported named input in inputs_cids: {}", cid));
                    };
                    ensure_task_not_cancelled(task, "resolving named input")?;
                    let meta_list = io
                        .find(&query_name, Some(data_type))
                        .await
                        .map_err(|e| anyhow!(e))
                        .with_context(|| format!("resolve named input {}", cid))?;
                    let Some(meta) = meta_list.into_iter().next() else {
                        return Err(anyhow!(
                            "named input {} resolved empty (name={} type={})",
                            cid,
                            query_name,
                            data_type
                        ));
                    };
                    info!(
                        data_id = %meta.id,
                        name = %meta.name,
                        data_type = %meta.data_type,
                        name_query = %query_name,
                        raw_input = %cid,
                        "resolved named input by name"
                    );
                    if refined_suffix.is_none() {
                        refined_suffix = extract_refined_suffix(Some(&meta.name));
                    }
                    let partial = materialize_inputs_for_mode(
                        mode,
                        &[DataMeta {
                            id: meta.id,
                            size: meta.size,
                            name: meta.name,
                            data_type: meta.data_type,
                        }],
                        &job_root,
                        task,
                        io,
                    )
                    .await
                    .with_context(|| format!("materialize named input {}", cid))?;
                    merge_download_summary(&mut summary, partial);
                    continue;
                }

                let materialized = io
                    .materialize(&cid)
                    .await
                    .with_context(|| format!("materialize cid {}", cid))?;

                let metadata_json = json!({
                    "cid": materialized.cid,
                    "data_id": materialized.data_id,
                    "name": materialized.name,
                    "data_type": materialized.data_type,
                    "domain_id": materialized.domain_id,
                    "path": materialized.path,
                    "related_files": materialized.related_files,
                    "extracted_paths": materialized.extracted_paths,
                });
                info!(%cid, metadata = %metadata_json, "input cid metadata");
                let _ = task.log_event(json!({
                    "level": "info",
                    "message": "input cid metadata",
                    "cid": cid.as_str(),
                    "metadata": metadata_json
                }));

                if refined_suffix.is_none() {
                    refined_suffix = extract_refined_suffix(materialized.name.as_deref());
                }

                let partial =
                    stage_materialized_url_input(mode, &materialized, &job_root, task, io)
                        .await
                        .with_context(|| format!("stage url input {}", cid))?;
                merge_download_summary(&mut summary, partial);
            }

            if mode == CapabilityMode::ColmapV1 {
                materialize_colmap_binaries(refined_suffix.as_deref(), &job_root, task, io)
                    .await?;
            }

        let mut datasets_present = summary.datasets_downloaded > 0;
        if !datasets_present {
            if let Ok(mut rd) = tokio::fs::read_dir(&datasets_dir).await {
                if rd.next_entry().await?.is_some() {
                    datasets_present = true;
                }
            }
        }
        if !datasets_present {
            return Err(anyhow!(
                "no datasets downloaded; expected at least one scan input"
            ));
        }

        // Resolve scripts at runtime so container layout is flexible.
        let pipeline_py = PathBuf::from("splatter_pipeline.py");
        let project_root = pipeline_py
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                env::var_os("SPLATTER_PROJECT_ROOT")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| PathBuf::from("/app"))
            });

        let mut cmd_args: Vec<String> = {
            let mode_name = match mode {
                CapabilityMode::ColmapV1 => "colmap_v1_single_splat",
                CapabilityMode::LocalV1 => "local_only",
                CapabilityMode::GlobalV1 => "global_only",
            };
            let mut args = vec![
                pipeline_py.display().to_string(),
                "--mode".to_string(),
                mode_name.to_string(),
                "--job_root_path".to_string(),
                job_root.display().to_string(),
                "--iterations".to_string(),
                splatter_iterations(),
            ];
            if !summary.scan_ids.is_empty() {
                args.push("--scan_ids".to_string());
                args.push(summary.scan_ids.join(","));
            }
            if !bool_env("SPLATTER_PARTITION", true) {
                args.push("--no_partition".to_string());
            } else {
                args.push("--partition".to_string());
            }
            if mode == CapabilityMode::GlobalV1 {
                args.push("--partition_size".to_string());
                args.push(splatter_partition_size());
            }
            args
        };

        if mode == CapabilityMode::GlobalV1 {
            if let Some(suffix) = refined_suffix.as_deref() {
                let ts = suffix.strip_prefix('_').unwrap_or(suffix);
                cmd_args.push("--manifest_timestamp".to_string());
                cmd_args.push(ts.to_string());
            }
        }

        task.progress(json!({
                "status": "running_python",
                "job_root_path": job_root,
                "capability": self.capability
            }))?;

        let full_cmd: Vec<String> = std::iter::once("python".to_string())
            .chain(cmd_args.iter().cloned())
            .collect();
        info!(
            cmd = ?full_cmd,
            cwd = %project_root.display(),
            path_env = ?env::var("PATH").unwrap_or_else(|_| "<unset>".into()),
            "spawning python pipeline"
        );
        let _ = task.log_event(json!({
                "level": "info",
                "message": "spawning python command",
                "command": full_cmd,
                "cwd": project_root.display().to_string(),
            }));

        ensure_task_not_cancelled(task, "before python start")?;
        let cmd_for_error = full_cmd.join(" ");
        let mut command = Command::new("python3");
        process::isolate(&mut command);
        for arg in &cmd_args {
            command.arg(arg);
        }
        let mut child = command
            .env("PYTHONUNBUFFERED", "1")
            .current_dir(&project_root)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .with_context(|| format!(
                "spawn python pipeline failed: {} (cwd={}, PATH={:?})",
                cmd_for_error,
                project_root.display(),
                env::var("PATH").unwrap_or_else(|_| "<unset>".into())
            ))?;

        let start = Instant::now();
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| anyhow!("child missing stdout handle"))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| anyhow!("child missing stderr handle"))?;

        let mut stdout_reader = BufReader::new(stdout).lines();
        let mut stderr_reader = BufReader::new(stderr).lines();
        let mut tail: VecDeque<String> = VecDeque::with_capacity(200);
        let mut heartbeat_interval = tokio::time::interval(std::time::Duration::from_secs(30));
        heartbeat_interval.tick(); // consume the immediate first tick

        let mut last_stage = String::from("starting");
        let mut last_pct: u8 = 0;
        let mut last_detail = String::new();
        let mut last_progress_send = Instant::now();
        const TRAINING_PROGRESS_MIN_INTERVAL: std::time::Duration = std::time::Duration::from_secs(5);

        let mut stdout_done = false;
        let mut stderr_done = false;
        let cancellation = task.cancellation();
        while !stdout_done || !stderr_done {
            tokio::select! {
                _ = cancellation.cancelled() => {
                    process::terminate_group(&mut child, process::TERMINATE_GRACE).await;
                    return Err(anyhow!("{TASK_CANCELLED_PREFIX}: python execution"));
                }
                line = stdout_reader.next_line(), if !stdout_done => {
                    match line {
                        Ok(Some(l)) => {
                            if tail.len() == 200 { tail.pop_front(); }
                            tail.push_back(format!("stdout: {l}"));
                            info!(line = %l, "python stdout");

                            if let Some(prog) = parse_progress_line(&l) {
                                let stage_changed = prog.stage != last_stage;
                                last_stage = prog.stage;
                                if !prog.detail.is_empty() { last_detail = prog.detail; }
                                if let Some(p) = prog.pct {
                                    if last_stage == "training" {
                                        // Map raw training 0-100% into overall 20-85%
                                        last_pct = (20.0 + p as f64 * 0.65).min(85.0) as u8;
                                    } else {
                                        last_pct = p;
                                    }
                                }

                                let should_send = stage_changed
                                    || last_stage != "training"
                                    || last_progress_send.elapsed() >= TRAINING_PROGRESS_MIN_INTERVAL;
                                if should_send {
                                    last_progress_send = Instant::now();
                                    let _ = task.progress(json!({
                                        "status": "processing",
                                        "capability": self.capability,
                                        "stage": last_stage,
                                        "pct": last_pct,
                                        "detail": last_detail,
                                        "elapsed_s": start.elapsed().as_secs(),
                                    }));
                                }
                            } else if let Some((cur, total, loss)) = parse_training_progress(&l) {
                                let train_frac = if total > 0 { cur as f64 / total as f64 } else { 0.0 };
                                last_pct = (20.0 + train_frac * 65.0).min(85.0) as u8;
                                last_stage = "training".to_string();
                                last_detail = format!("{cur}/{total} loss={loss:.4}");
                                if last_progress_send.elapsed() >= TRAINING_PROGRESS_MIN_INTERVAL {
                                    last_progress_send = Instant::now();
                                    let _ = task.progress(json!({
                                        "status": "processing",
                                        "capability": self.capability,
                                        "stage": "training",
                                        "pct": last_pct,
                                        "iter": cur,
                                        "iter_total": total,
                                        "loss": loss,
                                        "elapsed_s": start.elapsed().as_secs(),
                                    }));
                                }
                            } else {
                                let _ = task.log_event(json!({
                                    "level": "info",
                                    "message": l,
                                    "source": "python_stdout"
                                }));
                            }
                        }
                        Ok(None) => stdout_done = true,
                        Err(err) => {
                            warn!(%err, "failed to read python stdout");
                            stdout_done = true;
                        }
                    }
                }
                line = stderr_reader.next_line(), if !stderr_done => {
                    match line {
                        Ok(Some(l)) => {
                            if tail.len() == 200 { tail.pop_front(); }
                            tail.push_back(format!("stderr: {l}"));
                            warn!(line = %l, "python stderr");
                            let _ = task.log_event(json!({
                                "level": "warn",
                                "message": l,
                                "source": "python_stderr"
                            }));
                        }
                        Ok(None) => stderr_done = true,
                        Err(err) => {
                            warn!(%err, "failed to read python stderr");
                            stderr_done = true;
                        }
                    }
                }
                _ = heartbeat_interval.tick() => {
                    let elapsed = start.elapsed();
                    let _ = task.progress(json!({
                        "status": "processing",
                        "capability": self.capability,
                        "stage": last_stage,
                        "pct": last_pct,
                        "detail": last_detail,
                        "elapsed_s": elapsed.as_secs(),
                    }));
                }
            }
        }

        let status = child.wait().await.with_context(|| "wait for python job")?;
        let duration = start.elapsed();

        if !status.success() {
            let summary_tail: Vec<String> = tail.into_iter().collect();
            let _ = task.log_event(json!({
                    "level": "error",
                    "message": "python job failed",
                    "status": status.code(),
                    "duration_ms": duration.as_millis(),
                    "tail": summary_tail,
                }));
            return Err(anyhow!(
                "python job failed: status={:?}",
                status.code()
            ));
        }

            let _ = task.log_event(json!({
                    "level": "info",
                    "message": "python job completed",
                    "status": status.code(),
                    "duration_ms": duration.as_millis(),
                }));

        match mode {
            CapabilityMode::ColmapV1 => {
                upload_colmap_result(task, io, &job_root, refined_suffix.as_deref()).await?;
            }
            CapabilityMode::LocalV1 | CapabilityMode::GlobalV1 => {
                upload_staged_outputs(task, io, &job_root).await?;
            }
        }

            task.progress(json!({
                    "status": "finished",
                    "capability": self.capability
                }))?;

            Ok(())
        }
        .await;

        if let Err(err) = &task_result {
            if is_task_cancelled_error(err) {
                let _ = task.progress(json!({
                    "stage": "cancelled",
                    "status": "cancelled",
                }));
                let _ = task.log_event(json!({
                    "level": "warn",
                    "stage": "cancelled",
                    "message": err.to_string(),
                }));
            } else {
                let _ = task.log_event(json!({
                    "level": "error",
                    "stage": "runner",
                    "message": err.to_string(),
                }));
            }
        }

        if !tasks_cleanup_disabled() {
            // Best-effort cleanup of this task workspace to avoid disk growth.
            if let Err(err) = tokio::fs::remove_dir_all(&job_root).await {
                warn!(job_root = %job_root.display(), %err, "failed to remove task workspace");
            }
        } else {
            info!(
                job_root = %job_root.display(),
                "task cleanup disabled; leaving workspace on disk"
            );
        }

        task_result
    }
}

fn extract_refined_suffix(name: Option<&str>) -> Option<String> {
    let Some(name) = name else { return None };
    if let Some(suffix) = name.strip_prefix("refined_manifest") {
        return Some(suffix.to_string());
    }
    None
}

fn normalize_scan_id(raw: &str) -> String {
    let base = raw.rsplit('/').next().unwrap_or(raw);
    let base = base.split('.').next().unwrap_or(base);
    let parts: Vec<&str> = base.split('_').collect();
    if parts.len() >= 3 {
        let tail = parts[parts.len() - 1];
        let is_uuidish = tail.len() >= 16 && tail.chars().all(|c| c.is_ascii_hexdigit() || c == '-');
        if is_uuidish {
            return parts[parts.len() - 3..parts.len() - 1].join("_");
        }
    }
    base.to_string()
}


async fn materialize_inputs_for_mode(
    mode: CapabilityMode,
    metadata: &[DataMeta],
    job_root: &Path,
    task: &TaskContext,
    io: &TaskIo,
) -> Result<DownloadSummary> {
    let mut summary = DownloadSummary::default();
    let mut scan_set: HashSet<String> = HashSet::new();

    for m in metadata {
        let data_type = m.data_type.as_str();
        match mode {
            CapabilityMode::ColmapV1 | CapabilityMode::LocalV1 => {
                if mode == CapabilityMode::ColmapV1
                    && (data_type == "refined_manifest_json"
                        || m.name.starts_with("refined_manifest"))
                {
                    let dest = job_root
                        .join("refined")
                        .join("global")
                        .join("refined_manifest.json");
                    download_item(io, m.id, m.size, &dest).await?;
                    let bytes = tokio::fs::read(&dest)
                        .await
                        .with_context(|| format!("read staged refined_manifest {}", dest.display()))?;
                    let partial =
                        expand_colmap_inputs_from_manifest_bytes(&bytes, job_root, task, io)
                            .await?;
                    summary.datasets_downloaded += partial.datasets_downloaded;
                    for scan_id in partial.scan_ids {
                        scan_set.insert(scan_id);
                    }
                    continue;
                }
                if is_recording_input(data_type, &m.name) {
                    let scan_id = normalize_scan_id(m.name.trim_start_matches("dmt_recording_"));
                    let dest = job_root.join("datasets").join(&scan_id).join("Frames.mp4");
                    let bytes = download_item(io, m.id, m.size, &dest).await?;
                    summary.datasets_downloaded += 1;
                    scan_set.insert(scan_id.clone());
                    task.log_event(json!({
                            "level": "info",
                            "message": "downloaded dmt recording",
                            "data_id": m.id.to_string(),
                            "scan_id": scan_id,
                            "bytes": bytes,
                            "dest": dest.display().to_string(),
                        }));
                    continue;
                }
                if (mode == CapabilityMode::LocalV1 || mode == CapabilityMode::ColmapV1)
                    && (data_type == "refined_scan_zip" || m.name.starts_with("refined_scan_"))
                {
                    let scan_id = normalize_scan_id(m.name.trim_start_matches("refined_scan_"));
                    let dest = job_root.join("datasets").join(&scan_id).join("RefinedScan.zip");
                    let bytes = download_item(io, m.id, m.size, &dest).await?;
                    summary.datasets_downloaded += 1;
                    scan_set.insert(scan_id.clone());
                    let _ = task.log_event(json!({
                            "level": "info",
                            "message": "downloaded refined scan zip",
                            "data_id": m.id.to_string(),
                            "scan_id": scan_id,
                            "bytes": bytes,
                            "dest": dest.display().to_string(),
                        }));
                }
            }
            CapabilityMode::GlobalV1 => {
                if data_type == "local_splat_sog" || m.name.starts_with("local_splat_sog_") {
                    let scan_id =
                        normalize_scan_id(m.name.trim_start_matches("local_splat_sog_"));
                    let sog_name = format!("splat_{}.filtered.sog", splatter_iterations());
                    let dest = job_root
                        .join("refined")
                        .join("local")
                        .join(&scan_id)
                        .join("splat")
                        .join(sog_name);
                    let bytes = download_item(io, m.id, m.size, &dest).await?;
                    let dataset_dir = job_root.join("datasets").join(&scan_id);
                    tokio::fs::create_dir_all(&dataset_dir).await?;
                    summary.datasets_downloaded += 1;
                    scan_set.insert(scan_id.clone());
                    let _ = task.log_event(json!({
                            "level": "info",
                            "message": "downloaded local splat sog",
                            "data_id": m.id.to_string(),
                            "scan_id": scan_id,
                            "bytes": bytes,
                            "dest": dest.display().to_string(),
                        }));
                    continue;
                }
                if data_type == "refined_manifest_json" || m.name.starts_with("refined_manifest") {
                    let dest = job_root
                        .join("refined")
                        .join("global")
                        .join("refined_manifest.json");
                    let bytes = download_item(io, m.id, m.size, &dest).await?;
                    let _ = task.log_event(json!({
                            "level": "info",
                            "message": "downloaded refined manifest",
                            "data_id": m.id.to_string(),
                            "bytes": bytes,
                            "dest": dest.display().to_string(),
                        }));
                }
            }
        }
    }

    let mut scan_ids: Vec<String> = scan_set.into_iter().collect();
    scan_ids.sort();
    summary.scan_ids = scan_ids;
    Ok(summary)
}

fn is_recording_input(data_type: &str, name: &str) -> bool {
    if data_type == "dmt_recording_mp4" || name.starts_with("dmt_recording_") {
        return true;
    }
    let dt = data_type.to_ascii_lowercase();
    let n = name.to_ascii_lowercase();
    (dt.contains("recording") && dt.contains("mp4"))
        || n.ends_with(".mp4")
        || n.contains("recording")
}

fn is_refined_scan_input(data_type: &str, name: &str) -> bool {
    data_type == "refined_scan_zip" || name.starts_with("refined_scan_")
}

fn is_direct_dataset_input(name: Option<&str>, data_type: Option<&str>) -> bool {
    let name = name.filter(|n| !n.is_empty()).unwrap_or("");
    let data_type = data_type.unwrap_or("");
    is_recording_input(data_type, name) || is_refined_scan_input(data_type, name)
}

fn scan_id_from_dataset_artifact(name: Option<&str>, data_type: Option<&str>) -> Option<String> {
    let name = name.filter(|n| !n.is_empty())?;
    let data_type = data_type.unwrap_or("");
    if is_recording_input(data_type, name) {
        return Some(normalize_scan_id(name.trim_start_matches("dmt_recording_")));
    }
    if is_refined_scan_input(data_type, name) {
        return Some(normalize_scan_id(name.trim_start_matches("refined_scan_")));
    }
    None
}

fn dataset_dest_path(
    job_root: &Path,
    scan_id: &str,
    name: Option<&str>,
    data_type: Option<&str>,
) -> Option<PathBuf> {
    let name = name.filter(|n| !n.is_empty()).unwrap_or("");
    let data_type = data_type.unwrap_or("");
    if is_recording_input(data_type, name) {
        return Some(job_root.join("datasets").join(scan_id).join("Frames.mp4"));
    }
    if is_refined_scan_input(data_type, name) {
        return Some(job_root.join("datasets").join(scan_id).join("RefinedScan.zip"));
    }
    None
}

async fn materialize_colmap_binaries(
    refined_suffix: Option<&str>,
    job_root: &Path,
    task: &TaskContext,
    io: &TaskIo,
) -> Result<()> {
    let Some(suffix) = refined_suffix else {
        warn!("refined suffix missing; skipping colmap binary materialization");
        return Ok(());
    };

    let expected_colmap = [
        ("colmap_frames_bin", "frames.bin"),
        ("colmap_images_bin", "images.bin"),
        ("colmap_cameras_bin", "cameras.bin"),
        ("colmap_points3d_bin", "points3D.bin"),
        ("colmap_rigs_bin", "rigs.bin"),
    ];

    let mut colmap_refs: HashMap<String, (Uuid, u64)> = HashMap::new();
    let mut missing: Vec<&str> = Vec::new();
    for (prefix, _) in expected_colmap {
        ensure_task_not_cancelled(task, "resolving colmap metadata")?;
        let expected_name = format!("{prefix}{suffix}");
        match io.find(&expected_name, None).await {
            Ok(meta_list) => {
                if let Some(meta) = meta_list.into_iter().next() {
                    info!(
                        name = %expected_name,
                        data_id = %meta.id,
                        "found colmap metadata"
                    );
                    colmap_refs.insert(prefix.to_string(), (meta.id, meta.size));
                } else {
                    missing.push(prefix);
                    warn!(name = %expected_name, "colmap metadata missing");
                }
            }
            Err(err) => {
                missing.push(prefix);
                warn!(
                    name = %expected_name,
                    error = %err,
                    "colmap metadata fetch failed"
                );
            }
        }
    }

    if missing.is_empty() {
        let dest_dir = job_root
            .join("refined")
            .join("global")
            .join("refined_sfm_combined");
        tokio::fs::create_dir_all(&dest_dir).await?;

        for (prefix, target_name) in expected_colmap {
            if let Some((data_id, size)) = colmap_refs.get(prefix) {
                ensure_task_not_cancelled(task, "downloading colmap binary")?;
                let dest_path = dest_dir.join(target_name);
                match download_item(io, *data_id, *size, &dest_path).await {
                    Ok(bytes) => {
                        let _ = task.log_event(json!({
                            "level": "info",
                            "message": "downloaded colmap binary",
                            "data_id": data_id.to_string(),
                            "bytes": bytes,
                            "dest": dest_path.display().to_string(),
                        }));
                        info!(
                            name = %prefix,
                            bytes = bytes,
                            dest = %dest_path.display(),
                            "downloaded colmap binary"
                        );
                    }
                    Err(err) => {
                        let _ = task.log_event(json!({
                            "level": "error",
                            "message": "failed to download colmap binary",
                            "data_id": data_id.to_string(),
                            "error": err.to_string(),
                        }));
                        warn!(
                            name = %prefix,
                            data_id = %data_id,
                            error = %err,
                            "failed to download colmap binary"
                        );
                    }
                }
            }
        }
    } else {
        let _ = task.log_event(json!({
            "level": "warn",
            "message": "missing colmap binaries",
            "suffix": suffix,
            "missing": missing,
        }));
        warn!(
            suffix = %suffix,
            missing = ?missing,
            "missing colmap binaries"
        );
        return Err(anyhow!(
            "missing required colmap binaries for suffix {}: {:?}",
            suffix,
            missing
        ));
    }
    Ok(())
}

async fn upload_colmap_result(
    _task: &TaskContext,
    io: &TaskIo,
    job_root: &Path,
    refined_suffix: Option<&str>,
) -> Result<()> {
    let splat_abs = job_root.join("refined").join("splatter").join("splat_rot.splat");
    if !splat_abs.exists() {
        return Err(anyhow!("expected output missing: {}", splat_abs.display()));
    }

    let upload_key = if let Some(suffix) = refined_suffix.filter(|s| !s.is_empty()) {
        if suffix.starts_with('_') {
            format!("refined_splat{suffix}")
        } else {
            format!("refined_splat_{suffix}")
        }
    } else {
        warn!("refined manifest suffix missing; uploading as refined_splat");
        "refined_splat".to_string()
    };

    io.put_domain_artifact(ArtifactRequest {
            rel_path: upload_key.as_str(),
            name: upload_key.as_str(),
            data_type: "splat_data",
            existing_id: None,
            content: ArtifactContent::File(&splat_abs),
        })
        .await
        .with_context(|| format!("upload {} as {}", splat_abs.display(), upload_key))?;

    Ok(())
}

fn split_name_and_dtype(filename: &str) -> Option<(String, String)> {
    let idx = filename.rfind('.')?;
    let (name, dtype) = filename.split_at(idx);
    let dtype = dtype.trim_start_matches('.');
    if name.is_empty() || dtype.is_empty() {
        return None;
    }
    Some((name.to_string(), dtype.to_string()))
}

async fn upload_staged_outputs(_task: &TaskContext, io: &TaskIo, job_root: &Path) -> Result<()> {
    let output_dir = job_root.join("output");
    if !output_dir.exists() {
        return Err(anyhow!("expected output directory missing: {}", output_dir.display()));
    }

    let mut uploaded = 0usize;
    let mut rd = tokio::fs::read_dir(&output_dir).await?;
    while let Some(entry) = rd.next_entry().await? {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let Some(filename) = path.file_name().and_then(|v| v.to_str()) else {
            continue;
        };
        let Some((name, data_type)) = split_name_and_dtype(filename) else {
            warn!(file = %filename, "skipping output without name.data_type convention");
            continue;
        };
        io.put_domain_artifact(ArtifactRequest {
                rel_path: filename,
                name: &name,
                data_type: &data_type,
                existing_id: None,
                content: ArtifactContent::File(&path),
            })
            .await
            .with_context(|| format!("upload staged output {}", path.display()))?;
        uploaded += 1;
    }

    if uploaded == 0 {
        return Err(anyhow!("no staged outputs found in {}", output_dir.display()));
    }
    Ok(())
}

#[cfg(test)]
mod product_contract_tests {
    use super::*;

    #[test]
    fn input_name_query_maps_artifact_names_by_mode() {
        assert_eq!(
            input_name_query(CapabilityMode::LocalV1, "dmt_recording_abc.mp4"),
            Some(("dmt_recording_abc".to_string(), "dmt_recording_mp4"))
        );
        assert_eq!(
            input_name_query(CapabilityMode::ColmapV1, "refined_scan_scan1"),
            Some(("refined_scan_scan1".to_string(), "refined_scan_zip"))
        );
        assert_eq!(
            input_name_query(CapabilityMode::GlobalV1, "refined_scan_scan1"),
            Some(("local_splat_sog_scan1".to_string(), "local_splat_sog"))
        );
        assert_eq!(
            input_name_query(CapabilityMode::GlobalV1, "refined_manifest_2026-01-01"),
            Some(("refined_manifest_2026-01-01".to_string(), "refined_manifest_json"))
        );
        assert_eq!(
            input_name_query(CapabilityMode::ColmapV1, "refined_manifest_2026-01-01"),
            Some(("refined_manifest_2026-01-01".to_string(), "refined_manifest_json"))
        );
        assert!(input_name_query(CapabilityMode::LocalV1, "local_splat_sog_x").is_none());
        assert!(input_name_query(CapabilityMode::LocalV1, "refined_manifest_x").is_none());
    }

    #[test]
    fn colmap_manifest_data_ids_derive_scan_ids() {
        let parsed = json!({
            "dataIDs": [
                "refined_scan_2026-09-10_11-41-36",
                "dmt_recording_2026-09-10_11-42-19.mp4",
                "https://example.com/ignore",
                "45914b14-f6b6-47f0-8212-f3d19a594e0a"
            ]
        });
        assert!(is_refined_manifest_input(
            Some("refined_manifest_2026-01-01"),
            Some("refined_manifest_json")
        ));
        assert!(!is_refined_manifest_input(None, None));
        assert_eq!(
            scan_ids_from_manifest_data_ids(&parsed),
            vec![
                "2026-09-10_11-41-36".to_string(),
                "2026-09-10_11-42-19".to_string(),
            ]
        );
        assert!(reject_non_manifest_data_ids_envelope(
            br#"{"dataIDs":["refined_scan_x"]}"#,
            "cid",
            None,
            None
        )
        .is_err());
        assert!(reject_non_manifest_data_ids_envelope(
            serde_json::to_vec(&parsed).unwrap().as_slice(),
            "cid",
            Some("refined_manifest_2026-01-01"),
            Some("refined_manifest_json")
        )
        .is_ok());
    }
}
