# Initiative: Splatter node v0.2 release

Status: active  
Tier: feature  
Stage: 7-implement  
Skips: _(none)_  
Last updated: 2026-10-27

Living document — update in place when later work changes earlier conclusions.  
**§1 Intent (feature) and §4 System design stay distinct — never merge.**

---

## 1. Intent

- **Customer / internal need:** Domain operators need a splatter node that can finish **local+global** on domains **larger than DMT’s ~10-scan colmap cap**, without the v0.1 time/RAM/VRAM blowups, with **RTX 50xx** support, and without the quality/perf hit from large floaters / overdraw seen in earlier geo experiments.
- **Framing:** Ship **splatter node v0.2** that (1) runs distributed **`/splatter/local/v1` → `/splatter/global/v1`**, (2) keeps backward-compatible **`/splatter/colmap/v1`** for stock DMT ≤10 scans, (3) trains on **official stable LichtFeld** (no custom \(L_d\) fork), (4) densifies with the **RoMa densification plugin** instead of Splattr preprocess, (5) ships **SOG-only** artifacts for faster upload/IO.
- **Why now:** Local+global plumbing and recipes already exist in-tree but are not a clean release; custom \(L_d\) / MoGe paths add cost without clear win; densify+masks+SOG+10 m tiles are the product shape operators want.
- **Success look like:**
  1. Published image tagged **v0.2** (or agreed RC) registers all three caps; colmap path still produces `refined_splat_*` for DMT.
  2. Grocery-scale (and ideally grocery-large / >10 scans) local→global completes without OOM; wall time and peak VRAM acceptable vs v0.1 colmap on small domains.
  3. Visual quality: fewer large floaters / less overdraw than the degraded geo trials; human “good enough to ship” on at least one multi-scan domain.
  4. Builds and runs on **RTX 50xx** (smoke) in addition to existing Ada/Ampere nodes.

**§1 locked** (2026-10-25).

## 2. Alternatives

- Stay on v0.1 colmap-only + DMT 10-scan cap — **rejected** (doesn’t scale; no 50xx path).
- Ship custom LichtFeld \(L_d\) fork — **rejected** (not official stable; quality/PSNR regressions; Docker must pin official tags only).
- Keep Splattr preprocess + drop densify — **rejected** (human prefers densify turbo results from UI).
- Replace `points3D` with dense cloud — **rejected** in favor of **`--init` dense PLY** (cleaner).
- **Decision:** proceed with v0.2 as framed in §1.

## 3. Priority

- **Decision:** top-of-queue for splatter-server release work (blocks domain-setup scale-out).
- Opportunity cost: pauses further recipe ablations (garden \(L_d\) sweeps, etc.) until this ships or parks.

## 4. System design

**Decision:** design change required (trainer recipe, densify+masks staging, crop CLI, SOG-only contracts, LFS pin, Docker).

### Products / codebases

| Area | Role |
|---|---|
| `splatter-server` | Caps, pipeline, Docker, train_helper, crop/mask staging, uploads |
| LichtFeld Studio **v0.5.4.2** | Official pin (no custom \(L_d\)) |
| Densification plugin (`shadygm/Lichtfeld-Densification-Plugin`) | RoMa dense init CLI; bake into image / invoke from node |
| DMT / DMS (out of repo) | Already can call local→global; colmap remains ≤10 path |

### Locked recipe (train)

| Knob | Value |
|---|---|
| LFS pin | **v0.5.4.2** |
| Strategy / cap | **mrnf** / **1M** |
| Iters / resize | **30000** / **-r 2** |
| Exposure | **on** |
| MoGe / SSI / \(L_d\) / prior normals | **off** |
| NC / flatten | **0.1** / **0.1** |
| `normal_start_fraction` | **LFS default (0.08)** — do not override |
| Densify LRs/regs | **stock LFS** |
| Dense init | Plugin **`turbo`**, **`num_refs=0.6`** → PLY → train **`--init`** |
| Crop | Node Python: **1% trim + 0.3 m pad** AABB → **`--cropbox x0,y0,z0,x1,y1,z1`** (no in-LFS auto_fit script) |
| Occlusion | Pure-black pixels → sidecar **`masks/`** (0=ignore, 255=keep); train **`--mask-mode ignore`** + **`--no-alpha-as-mask`**; do **not** drop whole frames for black rects |
| Export / upload | **SOG only** (local + global partitions); no product PLY/`.splat` on local/global |
| Global tiles | **10 m**; coarse/fine LOD split unchanged (sigmoid ≥0.6, min size 0.01) |

### Pipeline (local → global)

1. Stage recording + refined scan (images + sparse as today; **no Splattr preprocess**).
2. Generate black-pixel **masks/** sidecars where needed.
3. Run densify CLI (`turbo`, `num_refs=0.6`, masks if supported).
4. Compute crop AABB → pass `--cropbox` into train.
5. `train_helper`: LFS headless train with recipe + `--init dense.ply` + masks + cropbox → SOG.
6. Local post-filter (opacity logit -3.5, size/view gates — retain unless bounce) → upload **`local_splat_sog_*` only**.
7. Global: combine (Sim3) → coarse/fine → partition **10 m** → upload **`splat_partition_{coarse|fine}_*`.sog** only.

### Pipeline (colmap / v0.1 back-compat)

Same densify + train recipe on merged multi-scan colmap job. **Locked 2026-10-25:** colmap keeps **`.splat` → `refined_splat_*`** for DMT; local/global stay **SOG-only**.

### Boundaries

- **In scope:** recipe/helper/Docker pin; densify integrate; crop Python→CLI; masks; SOG-only local/global; 10 m; CAPABILITIES; RTX 50xx smoke.
- **Out of scope:** stock DMT UI changes; custom LFS fork; MoGe; LEP CI gate.

### Risks

- Densify wall time/VRAM on 40+ scan domains even at turbo+0.6 refs.
- `--init` + dense PLY semantics vs sparse COLMAP (must re-enable helper `--init`).
- Mask polarity / resize alignment with `-r 2`.
- RTX 50xx: pin alone may not fix arch — may need CUDA/image bump (escalate if smoke fails).
- Colmap `.splat` path must keep filter→rotate→convert contract while local/global drop PLY uploads.

**§4 locked** (2026-10-25).

## 5. Verification design

**Depth:** wiring + one real local→global + colmap smoke + 50xx smoke. Human visual gate.

### Acceptance

1. Image builds with LFS **v0.5.4.2** (no `--use-depth-distortion` required).
2. Local job: densify turbo runs; train gets `--init` + `--cropbox` + `--mask-mode ignore` when masks exist; uploads **SOG only**.
3. Global job: 10 m coarse/fine SOG partitions; no PLY product upload.
4. Colmap/v1: still completes; DMT-compatible artifact (per §4 colmap decision).
5. Human visual: multi-scan domain “ship quality”; floaters/overdraw not worse than v0.1 baseline.
6. RTX 50xx: node registers and finishes a small smoke job.

### Checks

| ID | Check |
|----|--------|
| V1 | Helper argv: no \(L_d\); NC/flatten 0.1; no start override; `--init` present when densify ran |
| V2 | Crop passed as `--cropbox` (not python-script auto_fit) |
| V3 | Masks under `masks/`; `--mask-mode ignore` |
| V4 | Local/global product uploads SOG-only; partition size 10 |
| V5 | Docker pin = v0.5.4.2 |
| V6 | Human E2E visual |
| V7 | 50xx smoke |

### Out of scope for verify

- Full grocery-large SLA; multi-scene LEP CI; public fleet rollout.

**§5 locked** (2026-10-25).

## 6. Work breakdown (AI-oriented)

| Id | Slice | Notes |
|----|-------|-------|
| S1 | Pin LFS v0.5.4.2; strip \(L_d\)/MoGe bake leftovers from Dockerfile + helper | V5 |
| S2 | Recipe: NC/flatten 0.1; start default; exposure on; stock densify; enable `--init` | V1 |
| S3 | Python crop AABB (1%+0.3 m) → `--cropbox`; remove auto_fit python-script path | V2 |
| S4 | Black-pixel → `masks/` + `--mask-mode ignore` (+ densify masks if easy) | V3 |
| S5 | Integrate densify plugin (turbo, num_refs=0.6) into local+colmap before train | V1 |
| S6 | SOG-only local uploads; global SOG-only; partition default **10 m**; CAPABILITIES | V4 |
| S7 | Colmap keeps `.splat`/`refined_splat_*`; densify+recipe shared | V4/colmap |
| S8 | Human E2E local→global visual | V6 |
| S9 | RTX 50xx smoke | V7 |

**Stop/escalate:** densify OOM; 50xx needs new CUDA base; DMT rejects SOG on colmap → bounce §4 colmap format.

**§6 locked** (2026-10-25).  
**Approvals:** **mass-approve S1–S9** + **Auto mode: on** (2026-10-25).

## 7. Implementation notes

- **Auto mode: on** (2026-10-25) — mass-approve remaining S1–S9; skip per-slice AskQuestion until done/parked/revoked.
- Chat decisions 2026-10-25: official LFS only; no \(L_d\); densify `--init`; masks not frame drop; SOG-only; 10 m; crop via `--cropbox`.
- Mask polarity locked: **0=ignore, 255=keep**.
- Colmap artifact: **`.splat` / `refined_splat_*`** (locked 2026-10-25).
- Praxis: one implementer → review → verify per slice; subagents on **composer-2.5** / `composer-2.5-fast`.
- **Auto (S8 build):** densify bake **skipped for now** (`SPLATTER_DENSIFY=0`); full `Dockerfile` image `splatter-server:v0.2-dev`.
- **S8 fail fix (2026-10-27):** `local_main` bool params shadowed `convert_splat` module → `AttributeError` on SOG convert. Renamed to `do_convert_splat` / `do_convert_sog`. Resubmit job `72501615-07ad-46e7-a6ae-a8866e4eff6c`.
- **S8 global fail (2026-10-27):** v0.4.3 `refined_manifest` has `portal_alignment_metrics` but no `scanAlignmentTransforms`. Locals OK; global combine hard-failed. Grocery-small refined SFMs already share a global frame (scan means meters apart) → `combine_splats` identity Sim3 fallback. Global-only retry job `5313a2ca-d31f-4243-be78-8d6acd56552a` completed (20 SOG partitions).
- **S8 human visual (2026-10-27):** **pass** — mp4 looks good; splat decent though sparse; looks correct. Densify still off (`SPLATTER_DENSIFY=0`) so sparsity expected.
- Residual: recon should restore writing `scanAlignmentTransforms` when non-identity; identity fallback is a splat-side guard.

## 8. Verification record

- **S1** (2026-10-25): review approve; verify **pass** (V5 pin v0.5.4.2; L_d off on helper+local_main; MoGe bake removed).
- **S2** (2026-10-25): review approve; verify **pass** (V1 NC/flatten 0.1; no start override; JSON L_d off; `--init` chain).
- **S3** (2026-10-25): review approve; verify **pass** (V2 `crop_aabb` 1%+0.3 m → `--cropbox`; auto_fit removed).
- **S4** (2026-10-25): review approve; verify **pass** (V3 black→`masks/` + `--mask-mode ignore`; no whole-frame black drop).
- **S5** (2026-10-25): review approve (after bake revise); verify **pass** static (densify turbo/0.6 → `--init`; Dockerfile clone+venv; unit tests; docker build not run).
- **S6** (2026-10-25): review approve; verify **pass** (V4 local/global SOG-only; partition default 10 m; CAPABILITIES; contract tests).
- **S7** (2026-10-25): review approve; verify **pass** (colmap `refined_splat_*` `.splat` kept; CAPABILITIES split; contract tests; S6 SOG tests still pass).
- **S8** (2026-10-27): human V6 **pass** — mp4 good; splat sparse but correct (densify off).

## Gate (stage 11)

- **Decision:** _(pending)_  
- **Notes:**
