# Initiative: Splat quality via local+global R2

Status: active  
Tier: feature  
Stage: 6-breakdown  
Skips: _(none)_  
Last updated: 2026-09-14

Living document — update in place when later work changes earlier conclusions.  
**§1 Intent (feature) and §4 System design stay distinct — never merge.**

---

## 1. Intent

- **Customer / internal need:** Domain setup still produces Gaussian splats that are not good enough (geometry / edge fidelity). Prior local+global work was paused for quality; LichtFeld R2 recipe now makes quality viable.
- **Framing:** Keep the already-implemented **local train → Sim3 align → global combine/partition** capabilities (`/splatter/local/v1`, `/splatter/global/v1`). Upgrade the trainer to **latest LichtFeld Studio** and the locked **R2** recipe via `train_helper.sh` (no `--exposure`; no custom `--config`; caller still passes `-i` as today). **Do not change** legacy `/splatter/colmap/v1` (stock DMT does not send local-refinement inputs on that path). Testing uses a **modified DMT** that triggers local + global with the right artifacts. Global product output for this effort: **partitioned SOG**.
- **Why now:** R2 locked on LichtFeld spike (2026-09-13); previous pause was quality, not architecture. Legacy colmap cannot carry local→global without DMT input changes, so ship quality on the capabilities that already have the right inputs.
- **Success look like:**
  1. Docker image builds with latest LichtFeld + `train_helper.sh` R2 defaults wired into **local** splat training.
  2. `/splatter/local/v1` trains per-scan locals; `/splatter/global/v1` combines/partitions and uploads **partitioned SOG** artifacts (local intermediates as today for the distributed handoff).
  3. On-disk layout stays compatible with the existing local/global job split (no deep path rewrites).
  4. `/splatter/colmap/v1` behavior unchanged.
  5. Visual/quality bar is “can resume the effort” — not necessarily multi-scene LEP CI yet.

**§1 locked** (re-lock after bounce, 2026-09-14).

## 2. Alternatives

- Change `/splatter/colmap/v1` to sequential local→global — **rejected**: stock DMT does not send local-refinement inputs; requires DMT change anyway.
- Wait for stock DMT to call local/global before touching trainer — slower quality feedback; human will use modified DMT instead.

## 3. Priority

- Unblock quality validation on local+global + R2; legacy colmap path is out of scope for this initiative.

## 4. System design

**Decision:** Design change required (trainer integration + global SOG defaults). **No change** to `/splatter/colmap/v1` capability contract or pipeline.

### Products / codebases

| Area | Role |
|---|---|
| `splatter-server` | `/splatter/local/v1` + `/splatter/global/v1` runners; Docker image; on-disk layout |
| `LichtFeld-Studio` (`research/gs-geometric-consistency-r2` or successor commit) | `train_helper.sh` + Studio binary (MoGe preprocess + R2 train) |
| Modified DMT (out of repo) | Triggers local then global with existing local/global inputs; **not** implemented here |

### Boundaries

- **In scope:** local training via `train_helper.sh`; image bake of matching LichtFeld; scene layout adapter; drop splatter `LICHTFELD_CONFIG*` / `--config` / `--ppisp` from the local train path; global default to **partitioned SOG** uploads; leave colmap capability untouched.
- **Out of scope:** stock DMT changes; changing `/splatter/colmap/v1`; LEP CI / `train_helper_summary` as a hard gate; deep job path rewrites.

### Trainer integration (local)

1. **Invoke** `train_helper.sh` from `local_main.train_splat` (replace direct Studio argv + JSON config).
2. **Defaults from splatter:** `-i 20000` (keep prior iteration default; override helper’s 30k), **`-r 2`** (resize; same as today’s `-r 2`).
3. **No custom config:** do not pass `LICHTFELD_CONFIG` / `LICHTFELD_CONFIG_VDA`; do not rely on scene `output/train_30k_1m_eval.json`. Recipe lives only in `train_helper.sh` (+ `-i` / `-r`).
4. **LichtFeld dependency (required):** extend `train_helper.sh` to accept `-r` / `--resize_factor` and forward to Studio (`-r` still valid in latest CLI). While touching the helper: **stop auto-appending** optional `--config` from `train_30k_1m_eval.json` (or gate it so product calls never pick it up). Pin Docker to a commit that includes this helper.
5. **Scene layout:** helper requires `<scene>/images`. Adapter maps existing `refined/local/<scan>/dense/` (or equivalent COLMAP root) so `images` (+ sparse) satisfy the helper without rewriting the rest of the job tree.
6. **Env:** `LFS_EXE` / `LICHTFELD_BIN` → Studio binary; helper path env (e.g. `LICHTFELD_TRAIN_HELPER`) defaulting to repo/image location. Progress: accept quieter `--quiet` train logs; keep best-effort progress events if any non-quiet lines remain.
7. **VDA / sparsity / `--init`:** not supported by `train_helper.sh`. Treat as out of this design path (leave flags inert or document unsupported for local R2).
8. **MoGe:** first-run model cache under `~/.lichtfeld/onnx/` — bake or pre-download in Docker so GPU nodes need no egress for the model.

### Global outputs

- Keep combine → partition pipeline.
- **Product upload (locked):** **SOG-only** partitioned artifacts (`splat_partition_sog_*`). Do **not** upload `.splat` partitions on `/splatter/global/v1` for this path.
- Flip runner defaults: enable SOG conversion; disable `.splat` conversion/upload for global (today `SPLATTER_CONVERT_TO_SOG` defaults false; `.splat` defaults true — invert for this capability).
- Local capability still stages/uploads local artifacts needed for the distributed handoff (unchanged contract).

### Interfaces (capabilities)

- `/splatter/local/v1` — inputs unchanged (`dmt_recording_*`, `refined_scan_*`); training implementation swaps to helper.
- `/splatter/global/v1` — inputs unchanged; outputs **partitioned SOG only**; update `CAPABILITIES.md` if defaults/docs diverge.
- `/splatter/colmap/v1` — **frozen**.

### Risks / unknowns

- Cross-repo: splatter cannot ship `-r` via helper until LichtFeld helper lands (or image vendors a patched script).
- Disk/GPU: sequential per-scan local on one GPU; MoGe + 20k mrnf/1M is heavier than current 20k mcmc path.
- Helper summary/LEP soft-fail needs cv2/research scripts — must not fail the job in production image (skip summary in image or keep soft-fail without deps).
- Filename coupling: helper writes `splat_${ITERATIONS}.ply`; local filter/global expect consistent `base_filename` — keep aligned at 20000.

**§4 locked** (2026-09-14): `train_helper.sh` + `-i` as today (default 20000) + `-r 2`; no custom config/exposure; global **SOG-only**; colmap frozen.

## 5. Verification design

**Depth (locked):** smoke/wiring + **one real local→global run** with human visual quality check. No LEP CI / golden-scene harness in v1.

### Acceptance (must pass)

1. **Image / helper wiring**
   - Docker image builds with pinned LichtFeld that includes extended `train_helper.sh` (`-r` supported; no auto `--config` on product path).
   - `local_main.train_splat` invokes helper as  
     `train_helper.sh -i <N> -r 2 <scene> <out>`  
     with default `N=20000` from existing `--iterations` / `SPLATTER_ITERATIONS` plumbing.
   - No `--config`, `--ppisp`, or `--exposure` on that path.

2. **Local capability**
   - `/splatter/local/v1` on a real (or modified-DMT) job produces per-scan `splat_20000.ply` (or `splat_<N>.ply` matching N) under the existing local layout and stages handoff artifacts as today.

3. **Global capability — SOG-only**
   - `/splatter/global/v1` combine → partition succeeds.
   - Uploaded/product artifacts include partitioned **SOG** (`splat_partition_sog_*`).
   - **No** partitioned `.splat` uploads on this path.
   - `/splatter/colmap/v1` still behaves as before (smoke: capability registration + no code-path regression on that mode, or “untouched diff” review).

4. **End-to-end quality gate (human)**
   - One full local→global run (modified DMT or equivalent fixture with `refined_scan_*` inputs).
   - Human inspects resulting partitioned SOG / renders: quality is **good enough to resume** the domain-setup effort (geometry/edges vs pre-pause baseline). Fail → bounce §4/recipe, not silent “ship anyway.”

### Checks agents can run (§9)

| ID | Check | How |
|----|--------|-----|
| V1 | Helper argv contract | Unit/integration or log assert: command line contains `-i`, `-r 2`; lacks `--config`/`--exposure`/`--ppisp` |
| V2 | Artifact names | After local: `splat_<iters>.ply` present; after global: `splat_partition_sog_*` present, no `splat_partition_*.splat` product upload |
| V3 | Colmap frozen | Diff/review: `/splatter/colmap/v1` pipeline unchanged |
| V4 | Docker build | Image build succeeds on CI or local GPU builder |
| V5 | Human E2E | Record run id / paths + pass/fail note on plan after visual check |

### Out of scope for v1 verify

- Multi-scene LEP / PSNR regression suite.
- Stock DMT (unmodified) calling local/global.
- Performance/SLA for MoGe+20k mrnf.

**§5 locked** (2026-09-14).

## 6. Work breakdown (AI-oriented)

Ordered; one implementer subagent per agent slice. Stop/escalate → `/bounce-plan` if helper pin fails, SOG conversion breaks global, or E2E quality fails Intent.

| ID | Title | Owner | Depends | Boundary | Context | Verify (§5) | Human after? |
|----|--------|-------|---------|----------|---------|-------------|--------------|
| **S1** | Image + helper pin | agent | — | Extend LichtFeld `train_helper.sh` (`-r` forward; no product auto-`--config`); pin Docker to that commit; bake helper path + MoGe cache; do **not** change `train_splat` yet | §4 trainer/image; LichtFeld-Studio tree | V4 (+ helper supports `-r`) | no |
| **S2** | Local → `train_helper` | agent | S1 | `train_splat` shells to helper with `-i <N>` (default 20000) + `-r 2`; scene `images/` adapter; drop `--config`/`--ppisp`/JSON; leave colmap path alone | `local_main.py`, runner env | V1, V2 (local ply) | no |
| **S3** | Global SOG-only | agent | S2 (soft: can parallel after S1 if careful) | `/splatter/global/v1` defaults: SOG on, `.splat` partitions off; docs/`CAPABILITIES.md`; colmap untouched | runner env, `global_main` / pipeline upload | V2 (SOG-only), V3 | no |
| **S4** | Human E2E quality | **human** | S1–S3 | One modified-DMT (or fixture) local→global run; visual “resume effort” bar | §5 V5 | V5 | **yes** (is the check) |

**Stop conditions:** helper/API flag drift; global still emits `.splat` products; quality fail on S4 → bounce recipe (§4), don’t widen to colmap.

**Approvals:** **mass-approve all remaining** (2026-09-14) — S1→S3 agent slices + S4 human; still one implementer per slice with review/verify between.

**§6 locked** (2026-09-14, tighter 3+human shape).

### Slice progress

- **S1** (2026-09-14): implement + review approve-with-nits + verify **pass** (V4 full image build deferred). Pin `8fe40cf` + vendored `scripts/train_helper.sh`; commit `scripts/` before CI.
- **S2** (2026-09-14): `train_splat` → helper; colmap legacy via `use_train_helper=False`; V1/V2 local **pass** (static). Fix global `base_filename` in S3.
- **S3** (2026-09-14): Global SOG-only + `splat_{iterations}` handoff; V2 global + V3 **pass**. Next: **S4 human E2E (V5)**.

## Notes / discovery

### Bounce log

- **2026-09-14:** During §4, learned stock/legacy colmap jobs do **not** receive `refined_scan_*` / `refined_manifest*`. Sequential local→global on `/splatter/colmap/v1` would require a DMT change anyway. Human: **leave legacy colmap unchanged**; ship via **local + global** and test with a **modified DMT**. Supersedes prior draft that routed quality through colmap, and the in-progress decision to change colmap uploads to partitioned SOG.

### What DMT hits today

- Node registers three capabilities: `/splatter/colmap/v1`, `/splatter/local/v1`, `/splatter/global/v1`.
- Stock product path uses `/splatter/colmap/v1` → `colmap_v1_single_splat`.
- Modified DMT (out of this repo’s implement slices) will hit `/splatter/local/v1` + `/splatter/global/v1` for this initiative’s verification.

### LichtFeld follow-on to consume

- Locked R2 recipe from `LichtFeld-Studio` initiative `lichtfeld-gs-geometric-consistency`.
- Root helper: `train_helper.sh` (MoGe preprocess + R2 train flags; helper’s own default iters is 30k / exposure off).
- **Product call (locked):** always pass `-i` from existing plumbing (default **20000**, as today) and `-r 2`; never `--exposure`; no `--config`.
