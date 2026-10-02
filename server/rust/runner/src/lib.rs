use anyhow::{anyhow, Context, Result};
use async_trait::async_trait;
use node_host::auki_sdk::DataListQuery;
use node_host::{
    process, telemetry, ArtifactContent, ArtifactRequest, NodeRunner, Router, TaskContext, TaskIo,
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

/// Capability advertised to DDS/DMS.
pub const CAPABILITY: &str = "/splatter/colmap/v1";

/// Returns a router populated with the splatter runner.
pub fn registry() -> Router {
    Router::new().register(HelloRunner)
}

pub struct HelloRunner;

fn tasks_cleanup_disabled() -> bool {
    match env::var("DISABLE_TASKS_CLEANUP") {
        Ok(v) => matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"),
        Err(_) => false,
    }
}

/// Stream a Domain item to `dest`; a failed transfer leaves no partial file behind.
async fn download_item(io: &TaskIo, id: Uuid, size: u64, dest: &Path) -> Result<u64> {
    let result = io.download_to(id, size, dest).await;
    if result.is_err() {
        let _ = tokio::fs::remove_file(dest).await;
        if let Some(parent) = dest.parent() {
            // Only removes the directory when the failed file was its sole entry.
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

#[async_trait]
impl NodeRunner for HelloRunner {
    fn capability(&self) -> &'static str {
        CAPABILITY
    }

    async fn run(&self, task: &TaskContext, io: &TaskIo) -> Result<()> {
        let spec = &task.task;
        let domain_id = io.domain_id();

        // Attach common task identifiers to every tracing event emitted in this task.
        let task_span = telemetry::task_span(
            spec.id,
            spec.job_id.unwrap_or_else(Uuid::nil),
            &spec.capability,
            domain_id,
        );
        let _span_guard = task_span.enter();

        let mut refined_suffix: Option<String> = None;
        let mut colmap_refs: HashMap<String, (Uuid, u64)> = HashMap::new();
        // Resolve task workspace root; default is relative "tasks" for local dev,
        // but Docker image sets TASKS_ROOT=/app/tasks to avoid cwd/permission issues.
        let task_root = env::var("TASKS_ROOT").unwrap_or_else(|_| "tasks".to_string());
        let job_root = PathBuf::from(task_root).join(spec.id.to_string());
        tokio::fs::create_dir_all(&job_root)
            .await
            .with_context(|| format!("create job root {}", job_root.display()))?;
        let datasets_dir = job_root.join("datasets");

        let task_result: Result<()> = async {
            ensure_task_not_cancelled(task, "before execution")?;
            task.progress(json!({
                "pct": 5,
                "stage": "workspace",
                "status": "prepared",
                "job_root": job_root.display().to_string(),
            }))?;
            let _ = task.log_event(json!({
                "level": "info",
                "stage": "workspace",
                "message": "workspace prepared",
                "task_id": spec.id,
                "job_id": spec.job_id,
            }));

            // If an input CID is provided, materialize it and set up job inputs.
            let maybe_cid = spec.inputs_cids.first().cloned();
            let expected_colmap = [
                ("colmap_frames_bin", "frames.bin"),
                ("colmap_images_bin", "images.bin"),
                ("colmap_cameras_bin", "cameras.bin"),
                ("colmap_points3d_bin", "points3D.bin"),
                ("colmap_rigs_bin", "rigs.bin"),
            ];
            let mut datasets_downloaded = 0usize;
            let mut downloaded_recordings: HashSet<String> = HashSet::new();
            let mut metadata_chunks = 0usize;
            let mut metadata_items = 0usize;
            let mut recording_download_failures = 0usize;
            let mut derived_download_failures = 0usize;
            let mut colmap_downloaded = 0usize;
            let mut colmap_failed = 0usize;

            let cid = maybe_cid
                .as_deref()
                .ok_or_else(|| anyhow!("no input cid provided; cannot run splatter job"))?;

            ensure_task_not_cancelled(task, "before input materialization")?;
            let materialized = io
                .materialize(cid)
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

            if let Some(name) = materialized.name.as_deref() {
                if let Some(suffix) = name.strip_prefix("refined_manifest") {
                    refined_suffix = Some(suffix.to_string());
                }
            }

            // Read primary artifact bytes.
            let bytes = tokio::fs::read(&materialized.path).await.with_context(|| {
                format!("read materialized path {}", materialized.path.display())
            })?;
            let _ = tokio::fs::remove_dir_all(&materialized.root_dir).await;

            // Parse JSON and extract dataIDs field.
            let parsed_json: Value = serde_json::from_slice(&bytes)
                .with_context(|| format!("parse input cid {} as JSON", cid))?;
            let data_ids = parsed_json.get("dataIDs").cloned().unwrap_or(Value::Null);
            let data_id_list: Vec<String> = match &data_ids {
                Value::Array(arr) => arr
                    .iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect(),
                _ => Vec::new(),
            };
            let derive_scan_id = |raw: &str| -> Option<String> {
                let trimmed = raw.trim();
                if trimmed.is_empty() {
                    return None;
                }
                if trimmed.starts_with("http://") || trimmed.starts_with("https://") {
                    return None;
                }
                if let Some(rest) = trimmed.strip_prefix("refined_scan_") {
                    return Some(rest.to_string());
                }
                if let Some(rest) = trimmed.strip_prefix("dmt_recording_") {
                    return Some(rest.trim_end_matches(".mp4").to_string());
                }
                if Uuid::parse_str(trimmed).is_ok() {
                    return None;
                }
                Some(trimmed.to_string())
            };

            info!(%cid, dataIDs = %data_ids, "parsed input dataIDs");

            // Fetch and print metadata for all dataIDs using domain metadata endpoint.
            if !data_id_list.is_empty() {
                // Only data UUIDs can be listed by id; names are resolved below.
                let id_list: Vec<Uuid> = data_id_list
                    .iter()
                    .filter_map(|raw| Uuid::parse_str(raw.trim()).ok())
                    .collect();

                // Try chunked requests to avoid oversized query strings.
                let chunk_size = 50;
                for chunk in id_list.chunks(chunk_size) {
                    ensure_task_not_cancelled(task, "resolving dataset metadata")?;
                    metadata_chunks += 1;
                    let query = DataListQuery {
                        ids: chunk.to_vec(),
                        ..Default::default()
                    };
                    match io.list(&query).await {
                        Ok(metadata) => {
                            metadata_items += metadata.len();
                            if metadata.is_empty() {
                                warn!(
                                    chunk_len = chunk.len(),
                                    "metadata fetch returned empty chunk"
                                );
                            }
                            for m in metadata {
                                info!(
                                    data_id = %m.id,
                                    name = %m.name,
                                    data_type = %m.data_type,
                                    "dataID metadata"
                                );
                                if !m.name.starts_with("dmt_recording_") {
                                    continue;
                                }
                                if downloaded_recordings.contains(&m.name) {
                                    continue;
                                }
                                ensure_task_not_cancelled(task, "downloading input recording")?;
                                let folder_name = m
                                    .name
                                    .strip_prefix("dmt_recording_")
                                    .unwrap_or(m.name.as_str())
                                    .to_string();
                                let folder_name = if folder_name.is_empty() {
                                    m.id.to_string()
                                } else {
                                    folder_name
                                };
                                let dest_path = datasets_dir.join(&folder_name).join("Frames.mp4");
                                match download_item(io, m.id, m.size, &dest_path).await {
                                    Ok(bytes) => {
                                        datasets_downloaded += 1;
                                        downloaded_recordings.insert(m.name.clone());
                                        info!(
                                            data_id = %m.id,
                                            folder = %folder_name,
                                            bytes,
                                            dest = %dest_path.display(),
                                            "downloaded dmt recording"
                                        );
                                    }
                                    Err(err) => {
                                        recording_download_failures += 1;
                                        warn!(
                                            data_id = %m.id,
                                            error = %err,
                                            "failed to download dmt recording"
                                        );
                                    }
                                }
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

                // Derive dmt_recording_* names from scan IDs if needed.
                let mut unique_scan_ids: HashSet<String> = HashSet::new();
                for raw in &data_id_list {
                    let Some(scan_id) = derive_scan_id(raw) else {
                        continue;
                    };
                    if !unique_scan_ids.insert(scan_id.clone()) {
                        continue;
                    }
                    let candidate_names = [
                        format!("dmt_recording_{}", scan_id),
                        format!("dmt_recording_{}.mp4", scan_id),
                    ];
                    for recording_name in candidate_names {
                        if downloaded_recordings.contains(&recording_name) {
                            break;
                        }
                        ensure_task_not_cancelled(task, "resolving derived recording metadata")?;
                        match io.find(&recording_name, Some("dmt_recording_mp4")).await {
                            Ok(meta_list) => {
                                if let Some(meta) = meta_list.into_iter().next() {
                                    ensure_task_not_cancelled(
                                        task,
                                        "downloading derived recording",
                                    )?;
                                    let folder_name = if scan_id.is_empty() {
                                        meta.id.to_string()
                                    } else {
                                        scan_id.clone()
                                    };
                                    let dest_path =
                                        datasets_dir.join(&folder_name).join("Frames.mp4");
                                    match download_item(io, meta.id, meta.size, &dest_path).await {
                                        Ok(bytes) => {
                                            datasets_downloaded += 1;
                                            downloaded_recordings.insert(recording_name.clone());
                                            info!(
                                                data_id = %meta.id,
                                                folder = %folder_name,
                                                bytes,
                                                dest = %dest_path.display(),
                                                "downloaded derived dmt recording"
                                            );
                                            break;
                                        }
                                        Err(err) => {
                                            derived_download_failures += 1;
                                            warn!(
                                                data_id = %meta.id,
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
                                    error = %err,
                                    "failed to resolve derived dmt recording metadata"
                                );
                            }
                        }
                    }
                }

                if let Some(suffix) = refined_suffix.as_deref() {
                    colmap_refs.clear();
                    let mut missing = Vec::new();

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
                                match io.download_to(*data_id, *size, &dest_path).await {
                                    Ok(bytes) => {
                                        colmap_downloaded += 1;
                                        info!(
                                            name = %prefix,
                                            bytes,
                                            dest = %dest_path.display(),
                                            "downloaded colmap binary"
                                        );
                                    }
                                    Err(err) => {
                                        colmap_failed += 1;
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
                        colmap_failed += missing.len();
                        warn!(
                            suffix = %suffix,
                            missing = ?missing,
                            "missing colmap binaries"
                        );
                    }
                }
            }

            // Ensure at least one dataset is available before running the pipeline.
            let mut datasets_present = datasets_downloaded > 0;
            if !datasets_present {
                if let Ok(mut rd) = tokio::fs::read_dir(&datasets_dir).await {
                    if rd.next_entry().await?.is_some() {
                        datasets_present = true;
                    }
                }
            }
            if !datasets_present {
                return Err(anyhow!(
                    "no datasets downloaded; expected at least one dmt_recording_* input"
                ));
            }

            task.progress(json!({
                "pct": 20,
                "stage": "inputs",
                "status": "materialized",
                "datasets": datasets_downloaded,
                "metadata_chunks": metadata_chunks,
                "metadata_items": metadata_items,
                "recording_failures": recording_download_failures + derived_download_failures,
                "colmap_downloaded": colmap_downloaded,
                "colmap_failed": colmap_failed,
            }))?;
            let _ = task.log_event(json!({
                "level": "info",
                "stage": "inputs",
                "message": "inputs materialized",
                "datasets": datasets_downloaded,
                "metadata_chunks": metadata_chunks,
                "metadata_items": metadata_items,
                "recording_failures": recording_download_failures + derived_download_failures,
                "colmap_downloaded": colmap_downloaded,
                "colmap_failed": colmap_failed,
            }));

            // Run the Python pipeline and upload the splat.
            let domain_id_str = domain_id.to_string();

            let job_id_str = spec.job_id.map(|j| j.to_string()).unwrap_or_else(|| {
                // Fallback to task id so the script always has a value.
                spec.id.to_string()
            });

            // Resolve run.py path at runtime so the container layout is flexible.
            let exe_dir = env::current_exe()?
                .parent()
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("."));
            let run_py = env::var_os("SPLATTER_RUN_PY")
                .map(PathBuf::from)
                .unwrap_or_else(|| exe_dir.join("run.py"));
            let project_root = run_py
                .parent()
                .map(PathBuf::from)
                .unwrap_or_else(|| exe_dir.clone());

            ensure_task_not_cancelled(task, "before python start")?;
            task.progress(json!({
                "pct": 35,
                "stage": "python",
                "status": "starting",
                "job_root_path": job_root.display().to_string(),
                "script": run_py.display().to_string(),
            }))?;
            let _ = task.log_event(json!({
                "level": "info",
                "stage": "python",
                "message": "python pipeline starting",
                "script": run_py.display().to_string(),
            }));

            let mut command = Command::new("python3");
            let mut child = process::isolate(&mut command)
                .arg(&run_py)
                .arg("--domain_id")
                .arg(&domain_id_str)
                .arg("--job_id")
                .arg(&job_id_str)
                .arg("--job_root_path")
                .arg(&job_root)
                .arg("--log_level")
                .arg("info")
                .env("PYTHONUNBUFFERED", "1")
                .current_dir(&project_root)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .with_context(|| format!("spawn python3 {}", run_py.display()))?;

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
            let mut stdout_lines = 0usize;
            let mut stderr_lines = 0usize;
            let cancellation = task.cancellation();

            // Read both streams concurrently to avoid deadlocks and keep logs structured.
            let mut stdout_done = false;
            let mut stderr_done = false;
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
                                stdout_lines += 1;
                                info!(line = %l, "python stdout");
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
                                stderr_lines += 1;
                                warn!(line = %l, "python stderr");
                            }
                            Ok(None) => stderr_done = true,
                            Err(err) => {
                                warn!(%err, "failed to read python stderr");
                                stderr_done = true;
                            }
                        }
                    }
                }
            }

            let status = child.wait().await.with_context(|| "wait for python job")?;
            let duration = start.elapsed();

            if !status.success() {
                let summary_tail: Vec<String> = tail.into_iter().collect();
                let _ = task.progress(json!({
                    "pct": 35,
                    "stage": "python",
                    "status": "failed",
                }));
                let _ = task.log_event(json!({
                    "level": "error",
                    "stage": "python",
                    "message": "python job failed",
                    "status": status.code(),
                    "duration_ms": duration.as_millis(),
                    "stdout_lines": stdout_lines,
                    "stderr_lines": stderr_lines,
                    "tail": summary_tail,
                }));
                return Err(anyhow!("python job failed: status={:?}", status.code()));
            }

            task.progress(json!({
                "pct": 80,
                "stage": "python",
                "status": "completed",
                "duration_ms": duration.as_millis(),
            }))?;
            let _ = task.log_event(json!({
                "level": "info",
                "stage": "python",
                "message": "python pipeline completed",
                "status": status.code(),
                "duration_ms": duration.as_millis(),
                "stdout_lines": stdout_lines,
                "stderr_lines": stderr_lines,
            }));

            // Upload splat_rot.splat if it exists.
            ensure_task_not_cancelled(task, "before upload")?;
            let splat_rel = PathBuf::from("refined")
                .join("splatter")
                .join("splat_rot.splat");
            let splat_abs = job_root.join(&splat_rel);
            if !splat_abs.exists() {
                return Err(anyhow!("expected output missing: {}", splat_abs.display()));
            }

            let upload_key = if let Some(suffix) =
                refined_suffix.as_deref().filter(|s| !s.is_empty())
            {
                if suffix.starts_with('_') {
                    format!("refined_splat{suffix}")
                } else {
                    format!("refined_splat_{suffix}")
                }
            } else {
                warn!("refined manifest suffix missing; uploading as splat_data without timestamp");
                "refined_splat".to_string()
            };

            task.progress(json!({
                "pct": 90,
                "stage": "upload",
                "status": "starting",
                "artifact": upload_key.as_str(),
            }))?;

            io.put_domain_artifact(ArtifactRequest {
                rel_path: upload_key.as_str(),
                name: upload_key.as_str(),
                data_type: "splat_data",
                existing_id: None,
                content: ArtifactContent::File(&splat_abs),
            })
            .await
            .with_context(|| format!("upload {} as {}", splat_abs.display(), upload_key))?;

            task.progress(json!({
                "pct": 95,
                "stage": "upload",
                "status": "completed",
                "uploaded": upload_key.as_str(),
                "splat_path": splat_abs.display().to_string(),
            }))?;
            let _ = task.log_event(json!({
                "level": "info",
                "stage": "upload",
                "message": "output uploaded",
                "uploaded": upload_key.as_str(),
            }));
            task.progress(json!({
                "progress": 100,
                "stage": "complete",
                "status": "succeeded",
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
