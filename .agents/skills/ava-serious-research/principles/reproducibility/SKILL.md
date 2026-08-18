---
name: reproducibility
description: Use when starting any experiment, and whenever a number is about to be trusted — a result you cannot reproduce (including future you) is not a result yet; the record starts on day one, not at write-up time.
---

# Reproducibility

## One-sentence core
> The reproducibility record starts on day one, not at write-up time: seeds, configs, environment, logs, and intermediate artifacts are all kept, and every conclusion can be re-derived from the record.

## Core principles
- **Record before result**: any experiment establishes its record carrier (seed, config, environment, command) before running — **Why**: records reconstructed afterwards are inevitably distorted ("I changed a small thing"); Sandve et al.'s first rule of reproducible computational research is to record every step — **How**: day-one experiment directory template: `config.yaml + seeds.txt + env.lock + run.sh + logs/ + outputs/`.
- **Calibration checkpoint**: before the real experiment, reproduce a known result to prove the pipeline — **Why**: validate your instrument before measuring — no measurement discipline accepts readings from an unvalidated apparatus, and a research pipeline is an instrument. Reproducing a known result before trusting new ones is as old as experimental science; the 2026 pipeline evidence (calibration checkpoints as the key fault-tolerance mechanism) is in `ai-era/ai-research-landscape` — **How**: run the baseline's published numbers; only proceed to the real experiment when they match.
- **Seal the test set**: from the moment of the split, the test set is physically isolated and takes part in no computation until final evaluation — **Why**: the first of Kapoor's 8 leakage categories is lacking an independent test set; "split first, process second" is the primary defense — **How**: split script and training script are separate; test set archived under a hash; training paths cannot reach it.
- **Full trace**: code + logs + configs are archived together, not just the final script — **Why**: a number that cannot be re-derived is not yet a number — memory and write-ups drift, the record does not. Verification scales with the trace: the 2026 measurement (fabrication detection 55% from paper alone → 82% with logs + code, in `ai-era/ai-failure-modes`) quantifies how much checkability depends on the record — **How**: every run's command, stdout/stderr, random seed, and git commit are archived together.

## Checklist
- [ ] Experiment directory template created on day one (config/seeds/env/run/logs/outputs), before any run
- [ ] A known result was reproduced before the real experiments (calibration checkpoint passed)
- [ ] Test set split before preprocessing, physically isolated, hash-archived
- [ ] Every key number traces to a concrete run (command + log + seed)
- [ ] Environment is rebuildable (dependency lock or container); versions recorded
- [ ] Code and logs archived with results (not just the final script)
- [ ] A calibration-checkpoint run is archived with its match/mismatch result against the published number

## Anti-patterns
- **Record after the fact**: "recalling" configs at write-up time → instead: record at run time; the command is the documentation
- **Environment drift**: results change across machines/library versions without notice → instead: lock the environment + rerun calibration after any environment change
- **Only the final script survives**: intermediate artifacts, failed runs, and logs all lost → instead: archive everything; failed runs are data too
- **"Reproduction" that only reruns code, not process**: manual steps (tweaking parameters, picking seeds) unrecorded → instead: every variable input lives in config; the flow is scriptable
- **Documentation theater**: a beautiful README while the actual runs cannot be replayed → instead: replay is the test; the README describes how to replay, and the replay works

## Bad → good
- **bad**: changing the library version mid-project and not noticing the baseline numbers moved
- **good**: environment locked (env.lock) and the calibration checkpoint re-run after every environment change; drift is caught the day it happens, not at write-up
- **bad**: writing a README "to reproduce: run main.py" after the results table is done (which seed? which environment? what preprocessing?)
- **good**: the experiment directory holds `config.yaml` (all hyperparameters and split seed), `env.lock`, per-run `run.sh` and `logs/run-<ts>.log`; the README only says "execute the scripts in this directory in order"
- **bad**: baseline numbers don't match in a new environment, and the new method runs anyway
- **good**: on a new environment, first rerun the baseline's published numbers; only continue when they match; investigate environment differences otherwise

## Relationships
- Leakage defenses and split protocols: `practices/design`; statistical adjudication: `practices/measure`
- The sealed test set's hash is checked by the audit in `practices/verify`; the calibration checkpoint's pipeline evidence lives in `ai-era/ai-research-landscape`
- A collaborator or future-you re-derives the headline number from the archive alone — that is the test of the record
- Delivering the trace when presenting: `practices/present`; re-deriving key numbers when auditing: `practices/verify`
- With honesty: a distorted record is both a reproducibility problem and an integrity problem — `principles/honesty`

## Sources
- Sandve et al., Ten Simple Rules for Reproducible Computational Research
- Kapoor & Narayanan, Leakage and the Reproducibility Crisis in ML-based Science
- AI-era evidence (calibration checkpoints; trace-log detection 55%→82%): `ai-era/ai-research-landscape` + `ai-era/ai-failure-modes`
