# Initiative: Splatter node v0.2 release

Status: active  
Tier: feature  
Stage: 7-implement  
Skips: _(none)_  
Last updated: 2026-10-28 (S16 done; draft PR #36; S17 pending)

Living document — update in place when later work changes earlier conclusions.  
**§1 Intent (feature) and §4 System design stay distinct — never merge.**

---

## 1. Intent

- **Customer / internal need:** Domain operators need a splatter node that can finish **local+global** on domains **larger than DMT’s ~10-scan colmap cap**, without the v0.1 time/RAM/VRAM blowups, with **RTX 50xx** support, and without the quality/perf hit from large floaters / overdraw.
- **Framing:** Ship **splatter node v0.2** that (1) runs distributed **`/splatter/local/v1` → `/splatter/global/v1`**, (2) keeps backward-compatible **`/splatter/colmap/v1`** for stock DMT ≤10 scans, (3) trains on **official stable LichtFeld**, (4) uses **Splattr preprocess + short BA** (no RoMa densify), (5) trains under **max_cap 1M** with **POPSpa off**, (6) ships **SOG-only** local/global artifacts.
- **Why now:** Densify-on grocery compare was no clear win (removed). POPSpa 750k / 1.5M cap grocery compare looked **worse** than sparse baseline (human 2026-10-28) — drop POPSpa and return to **1M** cap.
- **Success look like:**
  1. Published image tagged **v0.2** registers all three caps; colmap still produces `refined_splat_*` for DMT.
  2. Grocery-scale local→global completes; wall time/VRAM acceptable.
  3. Visual quality competitive with S8 sparse baseline under 1M / no POPSpa; human ship gate.
  4. Builds and runs on **RTX 50xx**.

**§1 bounced** (2026-10-28 #2) — POPSpa no longer a success criterion; cap back to 1M.

## 2. Alternatives

- Stay on v0.1 colmap-only + DMT 10-scan cap — **rejected**.
- Ship custom LichtFeld \(L_d\) fork — **rejected**.
- Keep RoMa densify in product — **rejected** (grocery compare no clear win).
- POPSpa 750k / max_cap 1.5M — **rejected** (2026-10-28: human visual worse vs S8 sparse).
- **Decision:** v0.2 with Splattr+BA, no densify, **max_cap 1M**, **POPSpa off**, SOG local/global.

## 3. Priority

- **Decision:** top-of-queue for splatter-server release work.

## 4. System design

**Decision:** design change required (bounce 2026-10-28 #2).

### Products / codebases

| Area | Role |
|---|---|
| `splatter-server` | Caps, pipeline, Docker, train_helper, crop/mask, uploads |
| LichtFeld Studio **v0.5.4.2** | Official pin |
| DMT / DMS (out of repo) | local→global; colmap ≤10 |

**Removed from product:** RoMa densify plugin (`Lichtfeld-Densification-Plugin`), `densify_init`, VDA / MoGe / \(L_d\).  
**Default off (not product recipe):** POPSpa sparsity (`--enable-sparsity` remains optional CLI/env for experiments).

### Locked recipe (train)

| Knob | Value |
|---|---|
| LFS pin | **v0.5.4.2** |
| Strategy / max_cap | **mrnf** / **1M** |
| Iters / resize | **30000** / **-r 2** |
| Exposure | **on** |
| Train background | **`bg_mode=random`** (not solid black; avoids transparent-as-dark) |
| MoGe / SSI / \(L_d\) / prior normals | **off** |
| NC / flatten | **0.1** / **0.1** |
| `normal_start_fraction` | **LFS default (0.08)** |
| Train densify LRs/regs / max_screen_share | **stock LFS** |
| Dense init (`--init`) | **off** (no RoMa) |
| Preprocess | **Splattr** camera + point cleanup → undistort |
| Bundle adjust | **on** (≤20 iters; focal+radial; poses refineable) |
| POPSpa | **off** by default (`SPLATTER_ENABLE_SPARSITY=0`); optional CLI only |
| Crop | **1% trim + 0.3 m pad** → `--cropbox` |
| Occlusion | black pixels → `masks/` (0=ignore, 255=keep); `--mask-mode ignore` |
| Export / upload | **SOG only** local+global; colmap stays `.splat` |
| Global tiles | **10 m** |

### Pipeline (local → global)

1. Stage recording + refined scan.
2. Splattr preprocess (camera cull + sparse point cleanup) + **BA** → undistort `dense/`.
3. Black-pixel masks; crop AABB → `--cropbox`.
4. `train_helper`: recipe, **max_cap 1M**, **`--no-sparsity`**, masks, cropbox → SOG.
5. Local post-filter → upload `local_splat_sog_*`.
6. Global: combine → coarse/fine → 10 m SOG partitions.

### Pipeline (colmap)

Same train recipe (no densify); upload **`.splat` / `refined_splat_*`**.

### Boundaries

- **In scope:** default recipe flip (POPSpa off, max_cap 1M); CAPABILITIES/helper/docs; overlay rebuild + grocery smoke + visual.
- **Out of scope:** DMT UI; custom LFS fork; MoGe; LEP CI; bringing densify back; deleting optional sparsity CLI flags.

### Risks

- 1M cap alone may still haze vs densify experiments — accept unless human reopens densify.
- BA may nudge poses; keep iters low (already ≤20).

**§4 bounced** (2026-10-28 #2).

## 5. Verification design

**Depth:** static wiring + one local→global grocery smoke + human visual.

### Acceptance

1. No densify/VDA on product path; Dockerfile does not bake plugin.
2. Local: Splattr+BA; train argv has `--max-cap 1000000` and **no** `--enable-sparsity` / no `--popspa-target-count` by default.
3. Global: 10 m SOG partitions.
4. Colmap/v1 still produces `refined_splat_*`.
5. Human visual on grocery-small 1M / no-POPSpa.

### Checks

| ID | Check |
|----|--------|
| V1 | Helper: no \(L_d\); NC/flatten 0.1; no `--init`; **sparsity default off**; **max_cap 1M** |
| V2 | Crop `--cropbox` |
| V3 | Masks + `--mask-mode ignore` |
| V4 | SOG-only local/global; 10 m |
| V5 | Docker pin v0.5.4.2; no densify plugin bake |
| V6 | Human E2E visual |
| V8 | BA runs (bundle_adjust true on local path) |

**§5 bounced** (2026-10-28 #2).

## 6. Work breakdown

| Id | Slice | Notes |
|----|-------|-------|
| S1–S12 | _(done — historical)_ | densify era closed |
| S13–S15 | _(done / superseded)_ | densify removal + BA kept; POPSpa/1.5M recipe **superseded** by bounce #2 |
| **S16** | Recipe defaults: **max_cap 1M**; **POPSpa off** (`SPLATTER_ENABLE_SPARSITY` default 0 / `--no-sparsity`); CAPABILITIES + helper comments; keep optional `--enable-sparsity` for experiments; keep `--output-name` fix | V1 |
| **S17** | Overlay rebuild + grocery-small smoke + compare/visual (no POPSpa) | V6 |

**Stop/escalate:** visual still worse than S8 after S17.

**§6 bounced** (2026-10-28 #2).  
**Approvals:** Mass-approve S16–S17 (human 2026-10-28: proceed then commit + draft PR).

## 7. Implementation notes

- **Auto mode: on** — mass-approve S16–S17 (2026-10-28).
- **Bounce #1 (2026-10-28):** densify off; BA on; max_cap 1.5M; POPSpa 750k.
- **Bounce #2 (2026-10-28):** human visual on `splatter-v02-popspa` — POPSpa made it worse → **POPSpa off**, **max_cap 1M**. Keep densify out; keep BA.
- **Bounce #3 (2026-10-28):** train **`bg_mode=random`** (was solid_color/black); holes were reading as transparent instead of dark.
- Prior compare pages kept as historical evidence (`splatter-v02-densify`, `splatter-v02-popspa`, `splatter-v02-s8`).
- Praxis: implementer → review → verify per slice; subagents **composer-2.5** / `composer-2.5-fast`.
- **S15 smoke fail fix retained:** `train_helper` `--output-name splat_${ITERATIONS}` (harmless without POPSpa).

## 8. Verification record

- S1–S12: see prior entries (densify era).
- S13–S15: densify removed + BA on; POPSpa smoke completed then **rejected by human visual**.
- S16–S17: _(pending)_

## Gate (stage 11)

- **Decision:** _(pending — after S17)_  
- **Notes:** release recipe = Splattr+BA, no densify, no POPSpa, max_cap 1M.
