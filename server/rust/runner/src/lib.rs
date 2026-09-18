use anyhow::{anyhow, Context, Result};
use async_trait::async_trait;
use posemesh_compute_node::engine::RunnerRegistry;
use posemesh_compute_node::telemetry;
use posemesh_compute_node_runner_api as compute_runner_api;
use posemesh_domain_http::domain_data::{download_by_id, download_metadata_v1, DownloadQuery};
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

/// Returns a registry with all supported splatter capability runners.
pub fn registry() -> RunnerRegistry {
    RunnerRegistry::new()
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
    id: String,
    name: String,
    data_type: String,
    domain_id: String,
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
/// `Training [...] 45% [01m:30s<01m:50s] 9000/20000 | Loss: 0.0501`
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

/// Extract (domain_server_base, domain_id) from a full data CID URL.
fn parse_domain_from_cid(cid: &str) -> Option<(String, String)> {
    // Expect form: https://domain-server/api/v1/domains/{domain_id}/data/{data_id}
    let prefix = "/api/v1/domains/";
    let pos = cid.find(prefix)?;
    let base = cid[..pos].to_string();
    let rest = &cid[pos + prefix.len()..];
    let mut parts = rest.splitn(2, '/');
    let domain_id = parts.next()?.to_string();
    Some((base, domain_id))
}

#[async_trait]
impl compute_runner_api::Runner for SplatterRunner {
    fn capability(&self) -> &'static str {
        self.capability
    }

    async fn run(&self, ctx: compute_runner_api::TaskCtx<'_>) -> Result<()> {
        let lease = ctx.lease;

        // Attach common task identifiers to every tracing event emitted in this task.
        let task_span = telemetry::task_span(
            lease.task.id,
            lease.task.job_id.unwrap_or_else(Uuid::nil),
            &lease.task.capability,
            lease.domain_id.unwrap_or_else(Uuid::nil),
        );
        let _span_guard = task_span.enter();

        let token = ctx.access_token.get();
        let client_id =
            std::env::var("POSEMESH_CLIENT_ID").unwrap_or_else(|_| "splatter-runner".into());
        let mut refined_suffix: Option<String> = None;
        let mut domain_base_from_input: Option<String> =
            lease.domain_server_url.as_ref().map(|u| u.to_string());
        let mut domain_id_from_input: Option<String> = lease.domain_id.map(|d| d.to_string());
        let mode = match self.capability {
            CAPABILITY_COLMAP_V1 => CapabilityMode::ColmapV1,
            CAPABILITY_LOCAL_V1 => CapabilityMode::LocalV1,
            CAPABILITY_GLOBAL_V1 => CapabilityMode::GlobalV1,
            _ => return Err(anyhow!("unsupported capability {}", self.capability)),
        };
        // Resolve task workspace root; default is relative "tasks" for local dev,
        // but Docker image sets TASKS_ROOT=/app/tasks to avoid cwd/permission issues.
        let task_root = env::var("TASKS_ROOT").unwrap_or_else(|_| "tasks".to_string());
        let job_root = PathBuf::from(task_root).join(lease.task.id.to_string());
        tokio::fs::create_dir_all(&job_root)
            .await
            .with_context(|| format!("create job root {}", job_root.display()))?;
        let datasets_dir = job_root.join("datasets");

        let task_result: Result<()> = async {
            ctx.ctrl.progress(json!({ "status": "started" })).await?;

            // Materialize all input CIDs. Older /splatter/colmap/v1 jobs can provide
            // multiple input CIDs, so preserve that behavior for compatibility.
            let input_cids = lease.task.inputs_cids.clone();
            info!("input cids");
            let mut summary = DownloadSummary::default();

            if input_cids.is_empty() {
                return Err(anyhow!("no input cid provided; cannot run splatter job"));
            }

            for cid in input_cids {
                let materialized = ctx
                    .input
                    .materialize_cid_with_meta(&cid)
                    .await
                    .with_context(|| format!("materialize cid {}", cid))?;

            // Print metadata returned with the CID.
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
            ctx.ctrl
                .log_event(json!({
                    "level": "info",
                    "message": "input cid metadata",
                    "cid": cid.as_str(),
                    "metadata": metadata_json
                }))
                .await?;

            if refined_suffix.is_none() {
                refined_suffix = extract_refined_suffix(materialized.name.as_deref());
            }

            // Always prefer CID-derived domain routing for data metadata/download calls.
            // Lease-provided domain URL/ID can differ in some environments and cause
            // "route not found" on domain-data endpoints.
            if let Some((base, dom_id)) = parse_domain_from_cid(&cid) {
                domain_base_from_input = Some(base);
                domain_id_from_input = Some(dom_id);
            }

            // Read primary artifact bytes.
            let bytes = tokio::fs::read(&materialized.path).await.with_context(|| {
                format!("read materialized path {}", materialized.path.display())
            })?;

            // Parse JSON and extract dataIDs field.
            let parsed_json: Value = match serde_json::from_slice(&bytes) {
                Ok(v) => v,
                Err(err) if mode == CapabilityMode::ColmapV1 => {
                    // Backward compatibility: older colmap jobs may pass direct files
                    // rather than a dataIDs JSON envelope.
                    warn!(%cid, error = %err, "input cid was not a JSON envelope; skipping dataIDs parsing");
                    if let Some(copied_path) =
                        try_materialize_dataset_from_input_path(&materialized.path, &job_root).await?
                    {
                        summary.datasets_downloaded += 1;
                        let scan_id = copied_path
                            .parent()
                            .and_then(|p| p.parent())
                            .and_then(|p| p.file_name())
                            .map(|s| s.to_string_lossy().to_string())
                            .unwrap_or_else(|| "scan".to_string());
                        if !summary.scan_ids.contains(&scan_id) {
                            summary.scan_ids.push(scan_id.clone());
                        }
                        ctx.ctrl
                            .log_event(json!({
                                "level": "info",
                                "message": "materialized direct dataset input",
                                "cid": cid.as_str(),
                                "path": copied_path.display().to_string(),
                            }))
                            .await?;
                    }
                    continue;
                }
                Err(err) => {
                    return Err(err).with_context(|| format!("parse input cid {} as JSON", cid));
                }
            };
            let data_ids = parsed_json.get("dataIDs").cloned().unwrap_or(Value::Null);
            let data_id_list: Vec<String> = match &data_ids {
                Value::Array(arr) => arr
                    .iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect(),
                _ => Vec::new(),
            };
            info!(%cid, dataIDs = %data_ids, "parsed input dataIDs");
            ctx.ctrl
                .log_event(json!({
                    "level": "info",
                    "message": "parsed input json dataIDs",
                    "cid": cid.as_str(),
                    "dataIDs": data_ids
                }))
                .await?;

            if !data_id_list.is_empty() {
                if let (Some(domain_base_raw), Some(domain_id)) = (
                    domain_base_from_input.as_deref(),
                    domain_id_from_input.as_deref(),
                ) {
                    let domain_base = domain_base_raw.trim_end_matches('/').to_string();

                    // Try single-ID requests to avoid oversized query strings.
                    let chunk_size = 50;
                    let mut all_metadata = Vec::new();
                    let mut downloaded_recordings: HashSet<String> = HashSet::new();
                    for chunk in data_id_list.chunks(chunk_size) {
                        let query = DownloadQuery {
                            ids: chunk.to_vec(),
                            name: None,
                            data_type: None,
                        };
                        match download_metadata_v1(
                            &domain_base,
                            &client_id,
                            &token,
                            domain_id,
                            &query,
                        )
                        .await
                        {
                            Ok(metadata) => {
                                if metadata.is_empty() {
                                    warn!(
                                        chunk_len = chunk.len(),
                                        "metadata fetch returned empty chunk"
                                    );
                                }
                                for m in metadata {
                                    let name_matches = m.name.starts_with("dmt_recording_");
                                    if name_matches {
                                        if downloaded_recordings.contains(&m.name) {
                                            continue;
                                        }
                                        let data_id = m.id.clone();
                                        let domain_id_for_download = m.domain_id.clone();
                                        let folder_name = m
                                            .name
                                            .strip_prefix("dmt_recording_")
                                            .unwrap_or(m.name.as_str())
                                            .to_string();
                                        let folder_name = if folder_name.is_empty() {
                                            data_id.clone()
                                        } else {
                                            folder_name
                                        };
                                        match download_by_id(
                                            &domain_base,
                                            &client_id,
                                            &token,
                                            &domain_id_for_download,
                                            &data_id,
                                        )
                                        .await
                                        {
                                            Ok(bytes) => {
                                                let dest_dir = datasets_dir.join(&folder_name);
                                                tokio::fs::create_dir_all(&dest_dir).await?;
                                                let dest_path = dest_dir.join("Frames.mp4");
                                                tokio::fs::write(&dest_path, &bytes).await?;
                                                summary.datasets_downloaded += 1;
                                                downloaded_recordings.insert(m.name.clone());
                                                if !summary.scan_ids.contains(&folder_name) {
                                                    summary.scan_ids.push(folder_name.clone());
                                                }

                                                ctx.ctrl
                                                    .log_event(json!({
                                                        "level": "info",
                                                        "message": "downloaded dmt recording",
                                                        "data_id": data_id,
                                                        "folder": folder_name,
                                                        "bytes": bytes.len(),
                                                        "dest": dest_path.display().to_string(),
                                                    }))
                                                    .await?;
                                            }
                                            Err(err) => {
                                                ctx.ctrl
                                                    .log_event(json!({
                                                        "level": "warn",
                                                        "message": "failed to download dmt recording",
                                                        "data_id": data_id,
                                                        "error": err.to_string(),
                                                    }))
                                                    .await?;
                                                warn!(
                                                    data_id = %data_id,
                                                    error = %err,
                                                    "failed to download dmt recording"
                                                );
                                            }
                                        }
                                        continue;
                                    }
                                    info!(
                                        data_id = %m.id,
                                        name = %m.name,
                                        data_type = %m.data_type,
                                        "dataID metadata"
                                    );
                                    all_metadata.push(DataMeta {
                                        id: m.id,
                                        name: m.name,
                                        data_type: m.data_type,
                                        domain_id: m.domain_id,
                                    });
                                }
                            }
                            Err(err) => {
                                warn!(
                                    chunk_len = chunk.len(),
                                    error = %err,
                                    "metadata fetch failed for chunk"
                                );
                            }
                        }
                    }

                    // Fallback for inputs that provide refined_scan_* names instead of
                    // domain data IDs: derive the matching dmt_recording_* by name.
                    let mut seen_scan_ids: HashSet<String> = HashSet::new();
                    for raw in &data_id_list {
                        let scan_id = if let Some(rest) = raw.strip_prefix("refined_scan_") {
                            rest.to_string()
                        } else if let Some(rest) = raw.strip_prefix("dmt_recording_") {
                            rest.trim_end_matches(".mp4").to_string()
                        } else {
                            continue;
                        };
                        if scan_id.is_empty() || !seen_scan_ids.insert(scan_id.clone()) {
                            continue;
                        }
                        let candidate_names = [
                            format!("dmt_recording_{scan_id}"),
                            format!("dmt_recording_{scan_id}.mp4"),
                        ];
                        for candidate_name in candidate_names {
                            if downloaded_recordings.contains(&candidate_name) {
                                break;
                            }
                            let query = DownloadQuery {
                                ids: vec![],
                                name: Some(candidate_name.clone()),
                                data_type: Some("dmt_recording_mp4".to_string()),
                            };
                            match download_metadata_v1(
                                &domain_base,
                                &client_id,
                                &token,
                                domain_id,
                                &query,
                            )
                            .await
                            {
                                Ok(meta_list) => {
                                    if let Some(meta) = meta_list.into_iter().next() {
                                        let data_id = meta.id.clone();
                                        let domain_id_for_download = meta.domain_id.clone();
                                        let folder_name = scan_id.clone();
                                        match download_by_id(
                                            &domain_base,
                                            &client_id,
                                            &token,
                                            &domain_id_for_download,
                                            &data_id,
                                        )
                                        .await
                                        {
                                            Ok(bytes) => {
                                                let dest_dir = datasets_dir.join(&folder_name);
                                                tokio::fs::create_dir_all(&dest_dir).await?;
                                                let dest_path = dest_dir.join("Frames.mp4");
                                                tokio::fs::write(&dest_path, &bytes).await?;
                                                summary.datasets_downloaded += 1;
                                                downloaded_recordings.insert(candidate_name.clone());
                                                if !summary.scan_ids.contains(&folder_name) {
                                                    summary.scan_ids.push(folder_name.clone());
                                                }
                                                ctx.ctrl
                                                    .log_event(json!({
                                                        "level": "info",
                                                        "message": "downloaded derived dmt recording",
                                                        "data_id": data_id,
                                                        "folder": folder_name,
                                                        "bytes": bytes.len(),
                                                        "dest": dest_path.display().to_string(),
                                                        "name_query": candidate_name,
                                                    }))
                                                    .await?;
                                                break;
                                            }
                                            Err(err) => {
                                                warn!(
                                                    data_id = %data_id,
                                                    scan_id = %scan_id,
                                                    error = %err,
                                                    "failed to download derived dmt recording"
                                                );
                                            }
                                        }
                                    }
                                }
                                Err(err) => {
                                    warn!(
                                        scan_id = %scan_id,
                                        name_query = %candidate_name,
                                        error = %err,
                                        "failed metadata lookup for derived dmt recording"
                                    );
                                }
                            }
                        }
                    }

                    let partial = materialize_inputs_for_mode(
                        mode,
                        &all_metadata,
                        &domain_base,
                        &client_id,
                        &token,
                        &job_root,
                        &ctx,
                    )
                    .await?;
                    summary.datasets_downloaded += partial.datasets_downloaded;
                    for scan_id in partial.scan_ids {
                        if !summary.scan_ids.contains(&scan_id) {
                            summary.scan_ids.push(scan_id);
                        }
                    }

                    if mode == CapabilityMode::ColmapV1 {
                        materialize_colmap_binaries(
                            refined_suffix.as_deref(),
                            domain_base_from_input.as_deref(),
                            domain_id_from_input.as_deref(),
                            &client_id,
                            &token,
                            &job_root,
                            &ctx,
                        )
                        .await?;
                    }
                } else {
                    warn!(%cid, "could not resolve domain info from cid or lease");
                }
            }
        }

        // Keep backward compatibility for /splatter/colmap/v1: historically this
        // capability proceeded without this strict preflight and relied on pipeline-level
        // validation. Retain fail-fast checks for staged local/global capabilities.
        if mode != CapabilityMode::ColmapV1 {
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
        }

        // Run the Python pipeline and upload the splat.
            let Some(domain_id_str) = lease
                .domain_id
                .map(|d| d.to_string())
                .or(domain_id_from_input.clone())
            else {
                return Err(anyhow!(
                    "domain_id missing (task domain_id and input cid domain_id were None)"
                ));
            };

        let job_id_str = lease.task.job_id.map(|j| j.to_string()).unwrap_or_else(|| {
            // Fallback to task id so the script always has a value.
            lease.task.id.to_string()
        });

        // Resolve scripts at runtime so container layout is flexible.
        let exe_dir = env::current_exe()?
            .parent()
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
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
                env::var("SPLATTER_ITERATIONS").unwrap_or_else(|_| "20000".to_string()),
            ];
            if bool_env("SPLATTER_ENABLE_SPARSITY", false) {
                args.push("--enable_sparsity".to_string());
            }
            if bool_env("SPLATTER_REUSE_TRAINED", false) {
                args.push("--reuse_trained".to_string());
            }
            if !summary.scan_ids.is_empty() {
                args.push("--scan_ids".to_string());
                args.push(summary.scan_ids.join(","));
            }
            if bool_env("SPLATTER_CONVERT_TO_SOG", false) {
                args.push("--convert_to_sog".to_string());
            }
            if !bool_env("SPLATTER_CONVERT_TO_SPLAT", true) {
                args.push("--no_convert_to_splat".to_string());
            } else {
                args.push("--convert_to_splat".to_string());
            }
            if !bool_env("SPLATTER_PARTITION", true) {
                args.push("--no_partition".to_string());
            } else {
                args.push("--partition".to_string());
            }
            args
        };

        ctx.ctrl
            .progress(json!({
                "status": "running_python",
                "job_root_path": job_root,
                "capability": self.capability
            }))
            .await?;

        let full_cmd: Vec<String> = std::iter::once("python".to_string())
            .chain(cmd_args.iter().cloned())
            .collect();
        info!(
            cmd = ?full_cmd,
            cwd = %project_root.display(),
            path_env = ?env::var("PATH").unwrap_or_else(|_| "<unset>".into()),
            "spawning python pipeline"
        );
        ctx.ctrl
            .log_event(json!({
                "level": "info",
                "message": "spawning python command",
                "command": full_cmd,
                "cwd": project_root.display().to_string(),
            }))
            .await?;

        let cmd_for_error = full_cmd.join(" ");
        let mut child = Command::new("python")
            .args(cmd_args.drain(..))
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
        heartbeat_interval.tick().await; // consume the immediate first tick

        let mut last_stage = String::from("starting");
        let mut last_pct: u8 = 0;
        let mut last_detail = String::new();
        let mut last_progress_send = Instant::now();
        const TRAINING_PROGRESS_MIN_INTERVAL: std::time::Duration = std::time::Duration::from_secs(5);

        let mut stdout_done = false;
        let mut stderr_done = false;
        while !stdout_done || !stderr_done {
            tokio::select! {
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
                                    let _ = ctx.ctrl.progress(json!({
                                        "status": "processing",
                                        "capability": self.capability,
                                        "stage": last_stage,
                                        "pct": last_pct,
                                        "detail": last_detail,
                                        "elapsed_s": start.elapsed().as_secs(),
                                    })).await;
                                }
                            } else if let Some((cur, total, loss)) = parse_training_progress(&l) {
                                let train_frac = if total > 0 { cur as f64 / total as f64 } else { 0.0 };
                                last_pct = (20.0 + train_frac * 65.0).min(85.0) as u8;
                                last_stage = "training".to_string();
                                last_detail = format!("{cur}/{total} loss={loss:.4}");
                                if last_progress_send.elapsed() >= TRAINING_PROGRESS_MIN_INTERVAL {
                                    last_progress_send = Instant::now();
                                    let _ = ctx.ctrl.progress(json!({
                                        "status": "processing",
                                        "capability": self.capability,
                                        "stage": "training",
                                        "pct": last_pct,
                                        "iter": cur,
                                        "iter_total": total,
                                        "loss": loss,
                                        "elapsed_s": start.elapsed().as_secs(),
                                    })).await;
                                }
                            } else {
                                let _ = ctx.ctrl.log_event(json!({
                                    "level": "info",
                                    "message": l,
                                    "source": "python_stdout"
                                })).await;
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
                            let _ = ctx.ctrl.log_event(json!({
                                "level": "warn",
                                "message": l,
                                "source": "python_stderr"
                            })).await;
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
                    let _ = ctx.ctrl.progress(json!({
                        "status": "processing",
                        "capability": self.capability,
                        "stage": last_stage,
                        "pct": last_pct,
                        "detail": last_detail,
                        "elapsed_s": elapsed.as_secs(),
                    })).await;
                }
            }
        }

        let status = child.wait().await.with_context(|| "wait for python job")?;
        let duration = start.elapsed();

        if !status.success() {
            let summary_tail: Vec<String> = tail.into_iter().collect();
            ctx.ctrl
                .log_event(json!({
                    "level": "error",
                    "message": "python job failed",
                    "status": status.code(),
                    "duration_ms": duration.as_millis(),
                    "tail": summary_tail,
                }))
                .await?;
            return Err(anyhow!(
                "python job failed: status={:?}",
                status.code()
            ));
        }

            ctx.ctrl
                .log_event(json!({
                    "level": "info",
                    "message": "python job completed",
                    "status": status.code(),
                    "duration_ms": duration.as_millis(),
                }))
                .await?;

        match mode {
            CapabilityMode::ColmapV1 => {
                upload_colmap_result(
                    &ctx,
                    &job_root,
                    refined_suffix.as_deref(),
                    bool_env("SPLATTER_UPLOAD_DEBUG_ARTIFACTS", false),
                )
                .await?;
            }
            CapabilityMode::LocalV1 | CapabilityMode::GlobalV1 => {
                upload_staged_outputs(&ctx, &job_root).await?;
            }
        }

            ctx.ctrl
                .progress(json!({
                    "status": "finished",
                    "capability": self.capability
                }))
                .await?;

            Ok(())
        }
        .await;

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

async fn download_to(
    domain_base: &str,
    client_id: &str,
    token: &str,
    domain_id: &str,
    data_id: &str,
    dest_path: &Path,
) -> Result<usize> {
    let bytes = download_by_id(domain_base, client_id, token, domain_id, data_id).await?;
    if let Some(parent) = dest_path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    tokio::fs::write(dest_path, &bytes).await?;
    Ok(bytes.len())
}

async fn materialize_inputs_for_mode(
    mode: CapabilityMode,
    metadata: &[DataMeta],
    domain_base: &str,
    client_id: &str,
    token: &str,
    job_root: &Path,
    ctx: &compute_runner_api::TaskCtx<'_>,
) -> Result<DownloadSummary> {
    let mut summary = DownloadSummary::default();
    let mut scan_set: HashSet<String> = HashSet::new();

    for m in metadata {
        let data_type = m.data_type.as_str();
        match mode {
            CapabilityMode::ColmapV1 | CapabilityMode::LocalV1 => {
                if is_recording_input(data_type, &m.name) {
                    let scan_id = normalize_scan_id(m.name.trim_start_matches("dmt_recording_"));
                    let dest = job_root.join("datasets").join(&scan_id).join("Frames.mp4");
                    let bytes = download_to(domain_base, client_id, token, &m.domain_id, &m.id, &dest).await?;
                    summary.datasets_downloaded += 1;
                    scan_set.insert(scan_id.clone());
                    ctx.ctrl
                        .log_event(json!({
                            "level": "info",
                            "message": "downloaded dmt recording",
                            "data_id": m.id.as_str(),
                            "scan_id": scan_id,
                            "bytes": bytes,
                            "dest": dest.display().to_string(),
                        }))
                        .await?;
                    continue;
                }
                if mode == CapabilityMode::LocalV1
                    && (data_type == "refined_scan_zip" || m.name.starts_with("refined_scan_"))
                {
                    let scan_id = normalize_scan_id(m.name.trim_start_matches("refined_scan_"));
                    let dest = job_root.join("datasets").join(&scan_id).join("RefinedScan.zip");
                    let bytes = download_to(domain_base, client_id, token, &m.domain_id, &m.id, &dest).await?;
                    summary.datasets_downloaded += 1;
                    scan_set.insert(scan_id.clone());
                    ctx.ctrl
                        .log_event(json!({
                            "level": "info",
                            "message": "downloaded refined scan zip",
                            "data_id": m.id.as_str(),
                            "scan_id": scan_id,
                            "bytes": bytes,
                            "dest": dest.display().to_string(),
                        }))
                        .await?;
                }
            }
            CapabilityMode::GlobalV1 => {
                if data_type == "local_splat_ply" || m.name.starts_with("local_splat_ply_") {
                    let scan_id = normalize_scan_id(m.name.trim_start_matches("local_splat_ply_"));
                    let dest = job_root
                        .join("refined")
                        .join("local")
                        .join(&scan_id)
                        .join("splat")
                        .join("splat.filtered.ply");
                    let bytes = download_to(domain_base, client_id, token, &m.domain_id, &m.id, &dest).await?;
                    let dataset_dir = job_root.join("datasets").join(&scan_id);
                    tokio::fs::create_dir_all(&dataset_dir).await?;
                    summary.datasets_downloaded += 1;
                    scan_set.insert(scan_id.clone());
                    ctx.ctrl
                        .log_event(json!({
                            "level": "info",
                            "message": "downloaded local splat ply",
                            "data_id": m.id.as_str(),
                            "scan_id": scan_id,
                            "bytes": bytes,
                            "dest": dest.display().to_string(),
                        }))
                        .await?;
                    continue;
                }
                if data_type == "refined_manifest_json" || m.name.starts_with("refined_manifest") {
                    let dest = job_root
                        .join("refined")
                        .join("global")
                        .join("refined_manifest.json");
                    let bytes = download_to(domain_base, client_id, token, &m.domain_id, &m.id, &dest).await?;
                    ctx.ctrl
                        .log_event(json!({
                            "level": "info",
                            "message": "downloaded refined manifest",
                            "data_id": m.id.as_str(),
                            "bytes": bytes,
                            "dest": dest.display().to_string(),
                        }))
                        .await?;
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

async fn try_materialize_dataset_from_input_path(
    input_path: &Path,
    job_root: &Path,
) -> Result<Option<PathBuf>> {
    let ext = input_path
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let scan_id = input_path
        .file_stem()
        .and_then(|s| s.to_str())
        .map(normalize_scan_id)
        .unwrap_or_else(|| "scan".to_string());

    let dest = if ext == "mp4" {
        job_root.join("datasets").join(scan_id).join("Frames.mp4")
    } else if ext == "zip" {
        job_root.join("datasets").join(scan_id).join("RefinedScan.zip")
    } else {
        return Ok(None);
    };

    if let Some(parent) = dest.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    tokio::fs::copy(input_path, &dest).await?;
    Ok(Some(dest))
}

async fn materialize_colmap_binaries(
    refined_suffix: Option<&str>,
    domain_base: Option<&str>,
    domain_id: Option<&str>,
    client_id: &str,
    token: &str,
    job_root: &Path,
    ctx: &compute_runner_api::TaskCtx<'_>,
) -> Result<()> {
    let Some(suffix) = refined_suffix else {
        warn!("refined suffix missing; skipping colmap binary materialization");
        return Ok(());
    };
    let Some(domain_base) = domain_base else { return Ok(()); };
    let Some(domain_id) = domain_id else { return Ok(()); };

    let expected_colmap = [
        ("colmap_frames_bin", "frames.bin"),
        ("colmap_images_bin", "images.bin"),
        ("colmap_cameras_bin", "cameras.bin"),
        ("colmap_points3d_bin", "points3D.bin"),
        ("colmap_rigs_bin", "rigs.bin"),
    ];

    let mut colmap_refs: HashMap<String, (String, String)> = HashMap::new();
    let mut missing: Vec<&str> = Vec::new();
    for (prefix, _) in expected_colmap {
        let expected_name = format!("{prefix}{suffix}");
        let query = DownloadQuery {
            ids: vec![],
            name: Some(expected_name.clone()),
            data_type: None,
        };
        match download_metadata_v1(domain_base, client_id, token, domain_id, &query).await {
            Ok(meta_list) => {
                if let Some(meta) = meta_list.into_iter().next() {
                    info!(
                        name = %expected_name,
                        data_id = %meta.id,
                        "found colmap metadata"
                    );
                    colmap_refs.insert(prefix.to_string(), (meta.id, meta.domain_id.clone()));
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
            if let Some((data_id, domain_id_for_download)) = colmap_refs.get(prefix) {
                match download_by_id(domain_base, client_id, token, domain_id_for_download, data_id).await
                {
                    Ok(bytes) => {
                        let dest_path = dest_dir.join(target_name);
                        tokio::fs::write(&dest_path, &bytes).await?;

                        ctx.ctrl
                            .log_event(json!({
                                "level": "info",
                                "message": "downloaded colmap binary",
                                "data_id": data_id,
                                "bytes": bytes.len(),
                                "dest": dest_path.display().to_string(),
                            }))
                            .await?;

                        info!(
                            name = %prefix,
                            bytes = bytes.len(),
                            dest = %dest_path.display(),
                            "downloaded colmap binary"
                        );
                    }
                    Err(err) => {
                        ctx.ctrl
                            .log_event(json!({
                                "level": "error",
                                "message": "failed to download colmap binary",
                                "data_id": data_id,
                                "error": err.to_string(),
                            }))
                            .await?;
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
        ctx.ctrl
            .log_event(json!({
                "level": "warn",
                "message": "missing colmap binaries",
                "suffix": suffix,
                "missing": missing,
            }))
            .await?;
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
    ctx: &compute_runner_api::TaskCtx<'_>,
    job_root: &Path,
    refined_suffix: Option<&str>,
    upload_debug: bool,
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

    ctx.output
        .put_domain_artifact(compute_runner_api::runner::DomainArtifactRequest {
            rel_path: upload_key.as_str(),
            name: upload_key.as_str(),
            data_type: "splat_data",
            existing_id: None,
            content: compute_runner_api::runner::DomainArtifactContent::File(&splat_abs),
        })
        .await
        .with_context(|| format!("upload {} as {}", splat_abs.display(), upload_key))?;

    if upload_debug {
        let debug_dir = job_root.join("refined").join("splatter");
        if let Ok(mut rd) = tokio::fs::read_dir(&debug_dir).await {
            while let Some(entry) = rd.next_entry().await? {
                let p = entry.path();
                if !p.is_file() {
                    continue;
                }
                if p.file_name().and_then(|v| v.to_str()) == Some("splat_rot.splat") {
                    continue;
                }
                let Some(file_name) = p.file_name().and_then(|v| v.to_str()) else {
                    continue;
                };
                let key = format!("debug_{}", file_name.replace('.', "_"));
                let dtype = "debug_splat_artifact";
                let _ = ctx
                    .output
                    .put_domain_artifact(compute_runner_api::runner::DomainArtifactRequest {
                        rel_path: &key,
                        name: &key,
                        data_type: dtype,
                        existing_id: None,
                        content: compute_runner_api::runner::DomainArtifactContent::File(&p),
                    })
                    .await;
            }
        }
    }

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

async fn upload_staged_outputs(ctx: &compute_runner_api::TaskCtx<'_>, job_root: &Path) -> Result<()> {
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
        ctx.output
            .put_domain_artifact(compute_runner_api::runner::DomainArtifactRequest {
                rel_path: filename,
                name: &name,
                data_type: &data_type,
                existing_id: None,
                content: compute_runner_api::runner::DomainArtifactContent::File(&path),
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
