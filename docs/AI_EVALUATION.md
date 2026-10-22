# AI evaluation — 22 October 2026

Actual local CPU inference was rerun for both supplied checkpoints on exactly the same three public-domain source films. Every model/media digest was verified; application inference ran without remote downloads. The source and annotation manifests are `docs/ai/OFFLINE_EVALUATION_MANIFEST.json` and `docs/ai/OFFLINE_DEMO_ASSETS.json`. The staged positive is a scene from the 1903 train film, not actual dormitory surveillance. Boxing is a hard negative. Greeting is a normal-movement negative.

| Model | Staged positive event | Greeting false alerts | Boxing false alerts | Mean CPU window latency | Load time | Observed process RSS |
|---|---:|---:|---:|---:|---:|---:|
| Existing timm ViT | Missed | 0 | 1 | 0.184 s | 2.65 s | 761 MiB |
| VideoMAE demo | Detected | 0 | 1 | 0.371 s | 3.34 s | 867 MiB |

Each run evaluated 26 overlapping windows across three independent source films, at 4 sampled FPS, 5-second windows, 2.5-second stride, threshold 0.7, two confirmations and 60-second cooldown. VideoMAE's window confusion counts were TP10/FP12/TN4/FN0; ViT's were TP0/FP8/TN8/FN10. These are correlated descriptive observations with coarse annotations, not a representative test set. Window precision/recall/F1 and the tiny-duration false-alert extrapolation in the JSON are not field-accuracy claims. No threshold was changed based on this tiny dataset.

The real-time offline browser run observed an incident about 9 seconds after source start and a worker around 950 MiB RSS. The separate online-mode run observed 9.80 seconds from source start to incident. Library cache state, real-time video pacing, temporal confirmation, encoding and persistence explain why these differ from standalone per-window inference and media-time evaluation latency. The first readiness load on this Mac took 16.72 seconds; later runs used warmer caches. RSS snapshots are not maximum initialization or sustained Linux memory guarantees.

The selected **demonstration** adapter remains `cliffer1/videomae-base-finetuned-kinetics-violence-nonviolence-tuned`, revision `613844b1d2d27f68944619bf91885a9befb101ac`, strict safetensors SHA-256 `5166b055ebee1439d6517c5bf2d516dce7a1f65afc53367f43b5848970853d53`. Its two labels are NonViolence/Violence; the UI treats its output as suspicion requiring review. It produces no localized person boxes or identity claims. It is limited to CPU by the existing demo adapter; MPS/CUDA comparisons were not run.

The [pinned publisher card](https://huggingface.co/cliffer1/videomae-base-finetuned-kinetics-violence-nonviolence-tuned/blob/613844b1d2d27f68944619bf91885a9befb101ac/README.md) declares **CC BY-NC 4.0**, an unknown training dataset and incomplete intended-use details. Its live pinned text was retrieved and matched the bundled card's SHA-256 `0d2adb5b283c3c4bd8fd087d5ce85d0c2a6528c7ca6cc6ff65da51645452614f`. Attribution: cliffer1 fine-tune and MCG-NJU VideoMAE base. No commercial or institutional deployment clearance is inferred. No model was trained, fine-tuned, silently substituted or published in GitHub.

Representative modern fight/social/sports/crowd/low-light/occlusion evaluation remains unavailable. Retain this candidate for a clearly labeled noncommercial demonstration; do not claim deployment-grade detection quality. Earlier model research is preserved in `docs/ai/` and remains historical.

Reproduce the fresh comparison:

```sh
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python scripts/evaluate_dataset.py \
  --dataset docs/ai/OFFLINE_EVALUATION_MANIFEST.json \
  --media-root test-data/public-evaluation \
  --model-manifest docs/ai/OFFLINE_VIDEOMAE_MANIFEST.json \
  --evaluation-authorized --model-rights-confirmed --noncommercial-demo \
  --thresholds 0.7 --output /private/tmp/dormguard-videomae-evaluation.json
```

For ViT, use `checkpoints/jaranohaal-vit/manifest.json` and omit `--noncommercial-demo`. Raw reproducible scores and timings are preserved locally, excluded from GitHub, in `docs/engineering/OFFLINE_VIDEOMAE_RESULTS.json` and `OFFLINE_VIT_RESULTS.json`.
