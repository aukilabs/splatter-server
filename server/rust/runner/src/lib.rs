use anyhow::{anyhow, Context, Result};
use async_trait::async_trait;
use posemesh_compute_node::engine::RunnerRegistry;
use posemesh_compute_node::telemetry;
use posemesh_compute_node_runner_api as compute_runner_api;
use posemesh_domain_http::domain_data::{download_by_id, download_metadata_v1, DownloadQuery};
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::env;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Instant;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;
use tracing::{info, warn};
use uuid::Uuid;

/// Capability advertised to DDS/DMS.
pub const CAPABILITY: &str = "/splatter/colmap/v1";

/// Returns a registry populated with the hello runner.
pub fn registry() -> RunnerRegistry {
    RunnerRegistry::new().register(HelloRunner)
}

pub struct HelloRunner;

fn tasks_cleanup_disabled() -> bool {
    match env::var("DISABLE_TASKS_CLEANUP") {
        Ok(v) => matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"),
        Err(_) => false,
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
impl compute_runner_api::Runner for HelloRunner {
    fn capability(&self) -> &'static str {
        CAPABILITY
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
        let mut colmap_refs: HashMap<String, (String, String)> = HashMap::new();
        let mut domain_base_from_input: Option<String> = None;
        let mut domain_id_from_input: Option<String> = None;
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

            // If an input CID is provided, materialize it and set up job inputs.
            let maybe_cid = lease.task.inputs_cids.first().cloned();
            let expected_colmap = [
                ("colmap_frames_bin", "frames.bin"),
                ("colmap_images_bin", "images.bin"),
                ("colmap_cameras_bin", "cameras.bin"),
                ("colmap_points3d_bin", "points3D.bin"),
                ("colmap_rigs_bin", "rigs.bin"),
            ];
            let mut datasets_downloaded = 0usize;

            if let Some(cid) = maybe_cid.as_deref() {
                let materialized = ctx
                    .input
                    .materialize_cid_with_meta(cid)
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
                    "cid": cid,
                    "metadata": metadata_json
                }))
                .await?;

            if let Some(name) = materialized.name.as_deref() {
                if let Some(suffix) = name.strip_prefix("refined_manifest") {
                    refined_suffix = Some(suffix.to_string());
                }
            }

            if let Some((base, dom_id)) = parse_domain_from_cid(cid) {
                domain_base_from_input = Some(base);
                domain_id_from_input = Some(dom_id);
            }

            // Read primary artifact bytes.
            let bytes = tokio::fs::read(&materialized.path).await.with_context(|| {
                format!("read materialized path {}", materialized.path.display())
            })?;

            // Parse JSON and extract dataIDs field.
            let parsed_json: Value = serde_json::from_slice(&bytes)
                .with_context(|| format!("parse input cid {} as JSON", cid))?;
            let data_ids = parsed_json.get("dataIDs").cloned().unwrap_or(Value::Null);

            info!(%cid, dataIDs = %data_ids, "parsed input dataIDs");
            ctx.ctrl
                .log_event(json!({
                    "level": "info",
                    "message": "parsed input json dataIDs",
                    "cid": cid,
                    "dataIDs": data_ids
                }))
                .await?;

            // Fetch and print metadata for all dataIDs using domain metadata endpoint.
            if let Value::Array(arr) = data_ids {
                let ids: Vec<String> = arr
                    .iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect();
                if !ids.is_empty() {
                    if let Some((domain_base, domain_id)) = parse_domain_from_cid(cid) {
                        // Try single-ID requests to avoid oversized query strings.
                        let chunk_size = 50;
                        for chunk in ids.chunks(chunk_size) {
                            let query = DownloadQuery {
                                ids: chunk.to_vec(),
                                name: None,
                                data_type: None,
                            };
                            match download_metadata_v1(
                                &domain_base,
                                &client_id,
                                &token,
                                &domain_id,
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
                                        info!(
                                            data_id = %m.id,
                                            name = %m.name,
                                            data_type = %m.data_type,
                                            "dataID metadata"
                                        );
                                        let name_matches = m.name.starts_with("dmt_recording_");
                                        if name_matches {
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
                                                    datasets_downloaded += 1;

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

                                                    info!(
                                                        data_id = %data_id,
                                                        folder = %folder_name,
                                                        bytes = bytes.len(),
                                                        dest = %dest_path.display(),
                                                        "downloaded dmt recording"
                                                    );
                                                }
                                                Err(err) => {
                                                    ctx.ctrl
                                                        .log_event(json!({
                                                            "level": "error",
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

                        if let (Some(suffix), Some(domain_base), Some(domain_id)) = (
                            refined_suffix.as_deref(),
                            domain_base_from_input.as_deref(),
                            domain_id_from_input.as_deref(),
                        ) {
                            colmap_refs.clear();
                            let mut missing = Vec::new();

                            for (prefix, _) in expected_colmap {
                                let expected_name = format!("{prefix}{suffix}");
                                let query = DownloadQuery {
                                    ids: vec![],
                                    name: Some(expected_name.clone()),
                                    data_type: None,
                                };
                                match download_metadata_v1(
                                    domain_base,
                                    &client_id,
                                    &token,
                                    domain_id,
                                    &query,
                                )
                                .await
                                {
                                    Ok(meta_list) => {
                                        if let Some(meta) = meta_list.into_iter().next() {
                                            info!(
                                                name = %expected_name,
                                                data_id = %meta.id,
                                                "found colmap metadata"
                                            );
                                            colmap_refs.insert(
                                                prefix.to_string(),
                                                (meta.id, meta.domain_id.clone()),
                                            );
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
                                    if let Some((data_id, domain_id_for_download)) =
                                        colmap_refs.get(prefix)
                                    {
                                        match download_by_id(
                                            domain_base,
                                            &client_id,
                                            &token,
                                            domain_id_for_download,
                                            data_id,
                                        )
                                        .await
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
                            }
                        }
                    } else {
                        warn!(%cid, "could not parse domain info from cid");
                    }
                }
            }
            } else {
                return Err(anyhow!("no input cid provided; cannot run splatter job"));
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

            ctx.ctrl
                .progress(json!({
                    "status": "running_python",
                    "job_root_path": job_root,
                    "script": run_py
                }))
                .await?;

        let mut child = Command::new("python3")
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

        // Read both streams concurrently to avoid deadlocks and keep logs structured.
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
                            let _ = ctx.ctrl.log_event(json!({
                                "level": "info",
                                "message": l,
                                "source": "python_stdout"
                            })).await;
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

        // Upload splat_rot.splat if it exists.
        let splat_rel = PathBuf::from("refined")
            .join("splatter")
            .join("splat_rot.splat");
        let splat_abs = job_root.join(&splat_rel);
        if !splat_abs.exists() {
            return Err(anyhow!("expected output missing: {}", splat_abs.display()));
        }

        let upload_key = if let Some(suffix) = refined_suffix.as_deref().filter(|s| !s.is_empty()) {
            if suffix.starts_with('_') {
                format!("refined_splat{suffix}")
            } else {
                format!("refined_splat_{suffix}")
            }
        } else {
            warn!("refined manifest suffix missing; uploading as splat_data without timestamp");
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

            ctx.ctrl
                .progress(json!({
                    "status": "finished",
                    "uploaded": upload_key,
                    "splat_path": splat_abs,
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
