# Offline setup

Use Python 3.12 on Apple Silicon. The archive's old Python symlinks point to a different Mac; do not run them. This checkout has a reconstructed `.venv` using the local bundled Python 3.12.14 and the archived pinned packages, plus imageio-ffmpeg 0.6.0. `pip check` passes. A clean installation on another Mac remains a separate verification gate.

Connected installation:

```sh
./scripts/setup-demo.sh
```

Set `DORMGUARD_BOOTSTRAP_PYTHON` to a Python 3.12 executable if `python3.12` is unavailable. No host-specific absolute path is committed. Setup installs the pinned packages in `requirements-local.txt` and a private FFmpeg executable. The ordinary launch and readiness commands never install packages or download files.

Keep these private runtime assets outside Git:

- `checkpoints/cliffer-videomae/` matching every digest in `docs/ai/OFFLINE_VIDEOMAE_MANIFEST.json`.
- The three original films referenced by `docs/ai/OFFLINE_DEMO_ASSETS.json`, including the staged-conflict positive and two negatives.

The supplied archive includes these assets. The loader verifies all model hashes and uses strict local safetensors loading. Read the pinned publisher model card and the media manifests before confirming permitted noncommercial scope.

```sh
./scripts/check-offline-demo.sh --model-rights-confirmed
./scripts/start-demo.sh --model-rights-confirmed
```

Open `http://127.0.0.1:8877`. The launcher binds the API to loopback, isolates configuration from inherited provider secrets, and waits for `/ready` to confirm a fresh model worker and database. First loading on this Mac took 16.72 seconds; subsequent loads were shorter. Missing weights, media, runtime, disk space or port availability fail explicitly.

State lives in `.offline-demo/`. Ctrl-C stops children and preserves state. Restart with the same command to reopen it. In-app reset requires confirmation, creates a new demo generation, and preserves all prior evidence, audit records, source assets, model files and settings. It never resets unrelated data.

For the executed macOS acceptance method:

```sh
sandbox-exec -f scripts/offline-network.sb ./scripts/start-demo.sh \
  --model-rights-confirmed --workspace .offline-acceptance
node tests/offline_browser.cjs
```

The application/worker/FFmpeg children inherit OS denial of outbound networking except localhost. A direct external socket probe returned EPERM while localhost binding worked. The real Chrome test additionally aborts cross-origin requests. This profile may need an execution environment that permits creating a child macOS sandbox; the outer restricted agent sandbox did not.
