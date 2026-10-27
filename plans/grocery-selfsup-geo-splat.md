# Initiative: grocery-selfsup-geo-splat

Status: done  
Tier: spike  
Stage: 11-release  
Skips: 2–3 (ops run, not backlog ranking)  
Last updated: 2026-10-25

Living document — update in place when later work changes earlier conclusions.  
**§1 Intent (feature) and §4 System design stay distinct — never merge.**

---

## 1. Intent

- **Customer / internal need:** Validate the new self-supervised geo splatter recipe (L_d=100, NC=0.05, flatten=0.1, start=0.3, stock densify, no MoGe) on real grocery domains via product local+global splat.
- **Framing:** Run `/splatter/local/v1` + `/splatter/global/v1` on grocery small first, then grocery large. Reconstruct with RC2 only when refined caches are missing.
- **Why now:** Recipe just wired into splatter-server; need end-to-end domain evidence before TNT F1 / further tuning.
- **Success look like:** Grocery small uploads local_splat_ply + partitioned SOG; grocery large gets RC2 refined then same splat path; jobs complete without DMS dual-register conflicts.

## 2. Alternatives

- Stock DMT Rendering → `/splatter/colmap/v1` all-in-one (skips local→global product path).
- Offline train against downloaded refined zips (no DMS).
- **Decision:** proceed with DMS local+global DAG on dedicated node; colmap only as fallback if DAG create fails.

## 3. Priority

- **Decision:** top-of-queue for this chat (ops spike).

## 4. System design

- **Products:** splatter-server (recipe already wired), reconstruction-node RC2 image when needed, DEV DMT/DMS.
- **Constraints:** Same dedicated `REG_SECRET` cannot register recon + splat simultaneously → one-at-a-time on dedicated (recommended). Public `.env.splat-dev` will not pick dedicated jobs.
- **Interfaces:** DMS job DAG: N× `/splatter/local/v1` → 1× `/splatter/global/v1`; recon Dedicated send for grocery large if no `refined_scan_zip`.
- **Risks:** 41-scan large wall-clock; recipe needs L_d binary in image (r2 pin); bind-mount recipe over image to avoid full rebuild.
- **Decision:** no product code change beyond recipe already landed; ops orchestration only.

## 5. Verification design

- Splatter container registers and leases dedicated tasks.
- Grocery small: 4 local splat tasks + 1 global; `local_splat_ply_*` and `splat_partition_*_sog` appear on domain.
- Grocery large: RC2 recon produces `refined_manifest_*` + 41 `refined_scan_*`, then same splat path.
- Out of scope: PSNR/LEP report polish, TNT F1.

## 6. Work breakdown

| Id | Slice | Notes |
|----|-------|-------|
| S1 | DMS token strategy + stop dedicated recon | one-at-a-time |
| S2 | Launch splat with recipe binds + dedicated env | image `splatter-server:r2` |
| S3 | Send grocery small local+global splat | latest refined_manifest |
| S4 | After small done: RC2 recon grocery large | image `0.4.3-rc2-base-smoke` |
| S5 | Stop recon; splat grocery large local+global | same recipe |

## 7. Implementation notes

- **Auto mode: on** (2026-10-19) — mass-approve remaining S1–S5.
- Auto decisions:
  - DMS: **one-at-a-time dedicated** (not public splat env) — grocery jobs are Dedicated.
  - Recon image when needed: `reconstruction-node:0.4.3-rc2-base-smoke`.
  - Grocery small refined: reuse existing caches (4 zips + latest `refined_manifest_2026-10-19_12-22-14`); no recon.
  - Splatter launch: bind-mount host recipe files over `splatter-server:r2` + `SPLATTER_ITERATIONS=30000` (skip full rebuild).
  - Job shape: craft DMS DAG local/v1 + global/v1; fallback colmap/v1 if create fails.
- Residual risk: large 41×30k trains is multi-day GPU; will start and report progress.
- **2026-10-19 bounce:** stock `splatter-server:r2` LFS is mainline `8fe40cf2` (no L_d). Local L_d pin `3693dfb73` is **ahead of MrNeRF origin** (not fetchable). Building overlay `splatter-server:r2-ld` from local LFS tree. Job `08855890-…` failed on `--use-depth-distortion`; will resubmit after overlay.

## 8. Verification record

_(fill as jobs progress)_

## Gate (stage 11)

- **Accept** (2026-10-25): grocery small + large self-sup geo local+global splat complete; human done.
