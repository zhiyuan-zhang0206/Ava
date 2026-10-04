---
name: reproduce
description: "Builds experiment directories, locked environments, sealed test sets, and trace archives that re-derive every number. Use when starting an experiment, setting up its environment, or handing results to a collaborator, never only at write-up time."
---

# Reproduce: The Record

## One-sentence core
> The reproducibility record is built from day one, not at write-up time: a fixed experiment-directory template, a calibration checkpoint that proves the pipeline, a locked environment, a sealed test set, and a full trace of code + logs + configs so every number can be re-derived — and checked.

This skill is the How of `principles/reproducibility`. That principle says why the record matters and sets the invariants (record first, calibrate, seal, trace); this file gives the concrete templates and procedures.

## Core principles

- **Experiment directory from the first run**: scaffold the full template before running anything, so no artifact lands outside the record — **Why**: Sandve et al.'s first rule of reproducible computational research is "record every step"; a record reconstructed after the fact is inevitably distorted — the "small change" you made and forgot is exactly what breaks reproduction — **How**: create the tree below before the first run; the README states the run order so a fresh reader (or fresh machine) can follow it end to end.

  ```
  experiments/2026-08-06_attention-ablation/
  ├── config.yaml        # every hyperparameter + split seed + data path
  ├── seeds.txt          # one seed per line; run i uses seed i
  ├── env.lock           # pinned dependency versions (or container digest)
  ├── run.sh             # the exact command(s); args come from config only
  ├── logs/
  │   └── run-20260806-1430.log   # stdout+stderr, one per run
  ├── outputs/
  │   ├── metrics.json   # machine-readable numbers per run
  │   └── predictions/   # or pointer to the artifact store
  └── README.md          # run order: install → calibrate → run → evaluate
  ```

- **Everything variable lives in config**: no parameter edits inside code, no manual steps; the config is the single source of truth — **Why**: manual steps are the first thing lost in reproduction — rerunning the code is not rerunning the process; the process includes every mutable input (principles/reproducibility) — **How**: `run.sh` reads `config.yaml` and `seeds.txt`; each run records the config hash (or git commit) in its log line; if a parameter changes, the config changes and the run is a new entry, not an edit.
- **Calibration checkpoint before trusting the pipeline**: reproduce a known published number with your pipeline before any formal experiment — **Why**: validate your instrument before measuring — no measurement discipline accepts readings from an unvalidated apparatus, and if the pipeline is wrong every subsequent number is untrustworthy (the 2026 pipeline evidence, calibration checkpoints as a key fault-tolerance mechanism, is in `ai-era/ai-research-landscape`) — **How**: pick a published result your pipeline should match (baseline on a public benchmark), run it under the same protocol, and require agreement within tolerance (e.g. the published std); on mismatch, stop and investigate environment/config differences before proceeding; re-run the checkpoint after any environment change.
- **Environment locked**: pin dependency versions and record the platform — **Why**: environment drift silently changes results across machines and library updates, and the change is invisible unless the environment is recorded (Sandve et al.; the calibration checkpoint is the tripwire) — **How**: commit a lockfile (uv/poetry/pip-tools) or a container digest; record Python version, CUDA/toolchain, OS, and hardware in the experiment record; after any change, re-run the calibration checkpoint and log the result.
- **Test set sealed with a hash**: split before any preprocessing; the test set is physically unreachable from training paths and its hash is recorded — **Why**: the missing independent test set is leakage type #1 in Kapoor & Narayanan's taxonomy, and the most common cause of wildly overoptimistic results; a recorded hash proves no post-hoc re-split or tampering — **How**: the split script writes `test.csv` and a `split-manifest.json` with its sha256; the training pipeline asserts the hash on load and refuses to run on a mismatch; any change to the split produces a new hash and is recorded as a new experiment entry (see `practices/design` for the split protocol itself).
- **Full trace archived, not just the final script**: code + logs + configs + seeds + stdout/stderr archived together, failed runs included — **Why**: a number that cannot be re-derived is not yet a number — memory and write-ups drift, the record does not; the 2026 measurement (55% detection from paper alone → 82% with logs + code, `ai-era/ai-failure-modes`) shows verification scales with the trace, which is the only reliable substrate — **How**: every run appends its command, config hash, seed, and full stdout/stderr to `logs/`; failed and dropped runs are archived with the reason (OOM, crashed, discarded); the result report cites the run id, not just the number (see `practices/verify` for the audit side).

## Checklist
- [ ] Experiment directory scaffolded (config/seeds/env/run/logs/outputs + README) before the first run
- [ ] All hyperparameters, split seeds, and data paths live in config; no in-code or manual parameter edits
- [ ] Calibration checkpoint passed: a known published number reproduced within tolerance before formal runs
- [ ] Environment locked (lockfile or container); versions and platform recorded; checkpoint re-run after environment changes
- [ ] Test set split before any preprocessing; sha256 recorded; training code path asserts the hash and cannot reach the test set
- [ ] Every headline number cites a run id that maps to command + log + seed + config hash
- [ ] Failed and dropped runs archived with a recorded reason
- [ ] Code + logs + configs + seeds archived together (git commit or bundle) with each result
- [ ] README states the run order (install → calibrate → run → evaluate) and a fresh machine can follow it

## Anti-patterns
- **README-only reproducibility**: the results table is done, then a README says "reproduction: run main.py" — → Instead: the full directory template above; the README only points at scripts that already exist.
- **Unnoticed environment drift**: results change on a new machine and the project continues anyway — → Instead: locked environment + calibration checkpoint re-run on every environment change; mismatch is a stop condition.
- **Config scattered**: hyperparameters hardcoded across five files, some edited by hand mid-run — → Instead: one `config.yaml`, one `run.sh`; a changed parameter is a new run, not an edit.
- **Re-splitting the test set**: the split is re-run after tuning (hash changes, nobody notices) — → Instead: seal with sha256 at split time; the pipeline refuses to run on a different hash.
- **Only the final script saved**: intermediate outputs, failed runs, and logs discarded — → Instead: archive everything; failed runs are data (they are evidence in `principles/honesty`).
- **Calibration skipped "just this once"**: the baseline number is not verified because it is "a well-known result" — → Instead: the checkpoint is a pipeline test, not a trust test; it validates your code path, not the published result.

## Bad → good
- **bad**: `experiments/run_main.py` containing hardcoded hyperparameters, created two weeks after the results; the README claims "reproduction: run main.py" with no record of seeds, environment, or which runs produced which numbers.
- **good**:
  ```
  experiments/2026-08-06_attention-ablation/
  ├── config.yaml          # lr=3e-4, warmup=500, split_seed=42, data=...
  ├── seeds.txt            # 101 / 202 / 303 / 404 / 505
  ├── env.lock             # torch==2.5.1, transformers==4.46.2, ...
  ├── run.sh               # for seed in $(cat seeds.txt); do python train.py --config config.yaml --seed $seed; done
  ├── logs/run-*.log       # one per seed, each with config hash + seed + full stdout
  ├── outputs/metrics.json # mean±std over the 5 seeds
  └── README.md            # install from env.lock → run calibrate.sh → run.sh (array job) → evaluate.py
  ```
  Every number in the report cites `logs/run-<seed>.log`; the baseline published number was reproduced on this machine before the formal runs.
- **bad**: on a new machine the baseline accuracy comes out 2 points lower; the project proceeds anyway, and later nobody can tell whether the drop is environment or model.
- **good**: the mismatch is treated as a stop condition: dependency versions compared, the lockfile corrected, the calibration checkpoint re-run until the published number matches within tolerance; the resolution (e.g. torch 2.4 → 2.5 changed attention numerics) is logged in the experiment README.
- **bad**: the test split is re-generated after tuning experiments (the splitter is called again with a different seed); the reported test numbers drift and no one notices.
- **good**: `split.py` writes `split-manifest.json` with `{"test_sha256": "...", "split_seed": 42}`; `train.py` verifies the hash before touching data; any re-split fails loudly and is recorded as a new experiment entry with its own hash.

## Relationships
- The why behind all of this: `principles/reproducibility` (and `principles/honesty` for why failed runs stay on record)
- The split protocol this record seals: `practices/design`; the statistics that need the seeds: `practices/measure`
- The Eyeball/Blackbox split and Training-Dev sets from ML Yearning are reproducibility artifacts too — they keep the evaluation objective and diagnosable (`../../references/02-mlyearning.md`)
- The audit side — re-deriving numbers from this trace: `practices/verify`; delivering the trace to a human: `practices/present`

## Sources
- Sandve et al., *Ten Simple Rules for Reproducible Computational Research* — record every step; version control everything
- Kapoor & Narayanan, *Leakage and the Reproducibility Crisis in ML-based Science* — missing independent test set as leakage type #1; split-before-process (`../../references/05-kapoor.md`)
- Grounded Autonomous Research (arXiv:2607.02329) — calibration checkpoints with forced numeric comparison — evidence also in `ai-era/ai-research-landscape`
- Luo, Kasirzadeh, Shah, CMU evaluation of AI Scientist (arXiv:2509.08713) — detection 55% with paper only → 82% with trace logs + code — evidence also in `ai-era/ai-failure-modes`
- Ng, *Machine Learning Yearning* — Training-Dev protocol, Eyeball/Blackbox separation, learning curves as standard outputs (`../../references/02-mlyearning.md`)
