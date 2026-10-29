# Initiative: Splatter node v0.2 release

Status: active  
Tier: feature  
Stage: 7-implement (bounce #5 recipe) → smoke  
Skips: _(none)_  
Last updated: 2026-10-29 (bounce #5: prior normals + screen-share + NC dial-down; no \(L_d\) keys)

Living document — update in place when later work changes earlier conclusions.  
**§1 Intent (feature) and §4 System design stay distinct — never merge.**

---

## 1. Intent

- **Customer / internal need:** Domain operators need a splatter node that can finish **local+global** on domains **larger than DMT’s ~10-scan colmap cap**, without the v0.1 time/RAM/VRAM blowups, with **RTX 50xx** support, and without the quality/perf hit from large floaters / overdraw.
- **Framing:** Ship **splatter node v0.2** as a **clean product rewrite** that (1) runs distributed **`/splatter/local/v1` → `/splatter/global/v1`**, (2) keeps **v0.1-compatible `/splatter/colmap/v1` inputs and outputs** for stock DMT ≤10 scans, (3) trains on **official stable LichtFeld**,   (4) uses **Splattr preprocess + short BA** (no RoMa densify, no POPSpa), (5) trains under **max_cap 1M** with recipe owned by **config JSON**, including **prior normals (auto-generate)** + tighter screen-share, (6) ships **SOG-only** local/global artifacts.
- **Why now:** Densify and POPSpa grocery compares rejected; S19 1M baseline ready for human visual. Next: re-enable prior normals (auto-generate, no helper MoGe) and reduce oversized Gaussians via screen-share; dial NC down vs prior-era evidence.
- **Success look like:**
  1. Published image tagged **v0.2** registers all three caps; colmap still accepts the same inputs and produces `refined_splat_*` (v0.1 I/O).
  2. Grocery-scale local→global completes; wall time/VRAM acceptable.
  3. Visual quality competitive with S8 / S19 under **true** 1M + priors + screen-share; human ship gate.
  4. Builds and runs on **RTX 50xx**.
  5. No dead sparsity/env/densify experiment surface in product code.

**§1 bounced** (2026-10-29 #5) — prior normals + screen-share recipe on top of bounce #4 clean rewrite.

## 2. Alternatives

- Stay on v0.1 colmap-only + DMT 10-scan cap — **rejected**.
- Ship custom LichtFeld \(L_d\) fork — **rejected**.
- Keep RoMa densify in product — **rejected**.
- POPSpa 750k / max_cap 1.5M — **rejected** (human visual worse).
- Keep optional POPSpa CLI/env “for experiments” in product paths — **rejected** (2026-10-29: never shipped; leaves dead dual-control).
- **Decision:** clean v0.2; Splattr+BA; no densify; no POPSpa; **max_cap 1M in config**; helper defaults = product recipe; colmap v0.1 I/O only.

## 3. Priority

- **Decision:** top-of-queue for splatter-server release work.

## 4. System design

**Decision:** design change required (bounce 2026-10-29 #4).

### Products / codebases

| Area | Role |
|---|---|
| `splatter-server` | Caps, pipeline, Docker, train_helper, crop/mask, uploads |
| LichtFeld Studio **v0.5.4.2** | Official pin |
| DMT / DMS (out of repo) | local→global; colmap ≤10 |

**Removed from product (no back-compat with unshipped 0.2 experiments):** RoMa densify, VDA/MoGe preprocess, POPSpa / sparsity toggles, recipe env vars (`SPLATTER_ENABLE_SPARSITY`, `POPSPA_*`), dual CLI that fights config. Product config does **not** set \(L_d\) keys (pin has no \(L_d\)).

**Compatibility kept:** `/splatter/colmap/v1` **same inputs and outputs as v0.1** (notably `refined_splat_*` / `.splat` for DMT). Local/global are new v0.2 surfaces — no obligation to preserve interim experiment knobs.

### Recipe ownership

| Layer | Role |
|---|---|
| `config/lichtfeld_optimization_params.json` | **Source of truth** for train recipe (`max_cap=1M`, `bg_mode=random`, NC/flatten, strategy, iters, no sparsity keys) |
| `train_helper.sh` | Product default = run LFS with that config + scene/runtime only (`-d/-o`, cropbox, masks, `--output-name`, export). Optional flags for **local** experiments (`-i/-r`, `--ld`, `--init`, `--no-exposure`, …) — not wired through the node |
| Node (`local_main` / pipeline / Docker) | Not a recipe control plane — no sparsity/env recipe toggles; call helper with crop/masks/iters only |

### Locked recipe (train)

| Knob | Value |
|---|---|
| LFS pin | **v0.5.4.2** |
| Strategy / max_cap | **mrnf** / **1M** (in **config**) |
| Iters / resize | **30000** / **-r 2** |
| Exposure | **on** (config) |
| Train background | **`bg_mode=random`** (config) |
| SSI depth prior | **off** |
| Prior normals | **0.05** + **`normal_auto_generate=true`** (no helper MoGe; start fraction = LFS default **0.08**) |
| NC / flatten | **0.01** / **0.1** (config) |
| `max_screen_share` / oversize split | **0.2** / **0.25** |
| `min_opacity` | **0.01** |
| POPSpa / sparsity | **absent** (not in config; not in product CLI/env) |
| Dense init (`--init`) | **not used** on product path |
| Preprocess | **Splattr** + **BA** → undistort `dense/` |
| Crop / occlusion | cropbox + black-pixel masks |
| Export / upload | **SOG only** local+global; colmap stays `.splat` |
| Global tiles | **10 m** |

### Pipeline (local → global)

1. Stage recording + refined scan.
2. Splattr preprocess + BA → undistort `dense/`.
3. Masks + crop AABB → `--cropbox`.
4. `train_helper` with **config recipe** (+ crop/masks/output-name) → SOG.
5. Local post-filter → upload `local_splat_sog_*`.
6. Global: combine → coarse/fine → 10 m SOG partitions.

### Pipeline (colmap)

Same train recipe via config; upload **`.splat` / `refined_splat_*`** (v0.1-compatible outputs).

### Boundaries

- **In scope:** config-primary cleanup; delete sparsity/env/dead experiment wiring; CAPABILITIES/tests; overlay rebuild + grocery smoke + visual.
- **Out of scope:** DMT UI; custom LFS fork; MoGe; LEP CI; densify; preserving unshipped 0.2 experiment switches.

### Risks

- Stripping CLI overrides means LFS must honor config `max_cap` / no sparsity — verify in smoke (gaussian count stays ~1M).
- BA may nudge poses; keep iters low (≤20).

**§4 bounced** (2026-10-29 #4).

## 5. Verification design

**Depth:** static wiring + one local→global grocery smoke + human visual.

### Acceptance

1. Config has `max_cap=1000000`, `bg_mode=random`, **no** sparsity/POPSpa keys.
2. Product path does not read recipe env vars or pass sparsity / `--max-cap` CLI (cap comes from config).
3. Helper default argv does not enable sparsity; optional experiment flags are helper-local only.
4. Colmap/v1 still produces `refined_splat_*` (v0.1 I/O).
5. Grocery smoke finals ≈ **1M** Gaussians (not 750k); human visual vs S8.

### Checks

| ID | Check |
|----|--------|
| V1 | Config: max_cap 1M; no sparsity keys; bg_mode random |
| V2 | Helper/product: no SPLATTER_ENABLE_SPARSITY / POPSPA_* / --enable-sparsity |
| V3 | Crop `--cropbox` + masks |
| V4 | SOG-only local/global; 10 m |
| V5 | Docker: no densify plugin; no recipe sparsity ENV |
| V6 | Human E2E visual |
| V7 | Smoke metrics: ~1M gaussians at end (not 750k) |
| V8 | BA on; colmap `refined_splat_*` |

**§5 bounced** (2026-10-29 #4).

## 6. Work breakdown

| Id | Slice | Notes |
|----|-------|-------|
| S1–S17 | _(done / superseded)_ | densify/POPSpa experiment era closed; S17 smoke was **invalid** (JSON still sparsified) |
| **S18** | Config-primary cleanup: JSON recipe; strip sparsity/env/dead CLI from helper + `local_main` + pipeline + Docker; CAPABILITIES + contract tests; keep colmap v0.1 I/O | V1–V5, V8 |
| **S19** | Overlay rebuild + grocery-small smoke + compare/visual (prove ~1M, no POPSpa) | V6, V7 |
| **S20** | Recipe: priors 0.05 + auto-generate; NC 0.01 / flatten 0.1; screen 0.2 / split 0.25; min_opacity 0.01; no \(L_d\) keys; overlay + grocery smoke + compare vs S19 | V6, V7 |

**Stop/escalate:** smoke fails / VRAM OOM on auto-generate; visual worse than S19.

**§6 bounced** (2026-10-29 #5).  
**Approvals:** Auto mode on — human 2026-10-29 approved S20 implement + smoke.

## 7. Implementation notes

- **Auto mode: on** — mass-approve remaining.
- **Bounce #1–#3:** densify out; POPSpa rejected; bg_mode random (see git history / compare pages).
- **Bounce #4 (2026-10-29):** config is recipe source of truth; remove POPSpa/sparsity and recipe env; no back-compat with unshipped 0.2 experiments; only v0.1 colmap input/output compatibility; helper may keep local experiment flags but product defaults = config.
- **Bounce #5 (2026-10-29):** prior normals on (auto-generate); NC 0.01; screen-share 0.2/0.25; min_opacity 0.01; start fraction default 0.08; do not configure \(L_d\) keys in product JSON.
- **S19 pre-rebuild:** strip leftover product `--sparsify-steps` / `sparsify_steps=` (S18 verifier gap); leave `robin-experiments/` alone.
- Praxis: implementer → review → verify per slice; subagents **composer-2.5** / `composer-2.5-fast`.

## 8. Verification record

- S1–S15: densify/POPSpa experiments — closed / rejected.
- S16–S17: recipe flip attempted; S17 smoke **invalid** (config still POPSpa on → 750k).
- S18: **done** (static V1–V5, V8).
- S19: **done** — image `sha256:1e69206b0e67…`; job `adbecf72-…` GLOBAL_DONE; locals **1M** finals (not 750k); compare https://robin-ny1.tail2ecdfd.ts.net/splatter-v02-1m/ (`flythrough_1m.mp4` 48 390 959 B, MD5 `6D036EDA…`). Product sparsify strip complete.
- S20: **done** — image `sha256:d3dd7990b2a8…`; job `ffa25884-…` GLOBAL_DONE; locals 1M; PSNR 27.43 / 25.91 / 25.20 / 25.06 (vs S19 27.53 / 25.96 / 25.32 / 25.08); global fine ~3.10M. Compare https://robin-ny1.tail2ecdfd.ts.net/splatter-v02-s20/ (`flythrough_s20.mp4` ~53.7 MB).

## Gate (stage 11)

- **Decision:** _(pending — human visual after S20)_  
- **Notes:** S19 https://robin-ny1.tail2ecdfd.ts.net/splatter-v02-1m/ ; S20 https://robin-ny1.tail2ecdfd.ts.net/splatter-v02-s20/ (side-by-side + DMT notes).
