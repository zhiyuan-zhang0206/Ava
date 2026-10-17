# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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

## Sources
- Sandve et al., *Ten Simple Rules for Reproducible Computational Research* — record every step; version control everything
- Kapoor & Narayanan, *Leakage and the Reproducibility Crisis in ML-based Science* — missing independent test set as leakage type #1; split-before-process (`../../../references/05-kapoor.md`)
- Grounded Autonomous Research (arXiv:2607.02329) — calibration checkpoints with forced numeric comparison — evidence also in `ai-era/ai-research-landscape`
- Luo, Kasirzadeh, Shah, CMU evaluation of AI Scientist (arXiv:2509.08713) — detection 55% with paper only → 82% with trace logs + code — evidence also in `ai-era/ai-failure-modes`
- Ng, *Machine Learning Yearning* — Training-Dev protocol, Eyeball/Blackbox separation, learning curves as standard outputs (`../../../references/02-mlyearning.md`)
