# Protected online demonstration

## Architecture and current gate

Use one non-root CPU container, one replica, one private persistent volume, and provider-managed HTTPS. FastAPI serves the actual frontend and API; the supervisor starts the existing inference worker and exits if a child fails. SQLite, evidence, prepared test footage and pinned model files share `/app/data`. This avoids cross-service URLs, CORS, a separate frontend build, and the compromised historical Supabase credential. Existing production PostgreSQL/Supabase support is preserved.

**No public service has been provisioned.** Native online-mode HTTPS/browser/inference/restart checks pass locally; Docker build/run and public end-to-end checks remain separate. Docker is unavailable on this Mac. The repository includes `Dockerfile.online`, `compose.online.yaml` and schema-validated `railway.json`. The CPU image installs pinned PyTorch CPU wheels to avoid CUDA dependencies, never embeds model weights/private footage, and uses `/ready` for health.

## Resource and cost estimate

The local HTTPS run observed approximately 50 MiB API RSS plus 794 MiB worker RSS at one sampling point; the offline worker reached about 950 MiB in its observed metrics. These are observations, not peak allocator guarantees or Linux capacity measurements. Start with **2 vCPU, 3 GB RAM and 5 GB persistent storage**, one replica. Benchmark again on the selected Linux instance before accepting capacity.

[Railway pricing](https://railway.com/pricing), checked 22 October 2026, lists a $5/month Hobby minimum, memory at $0.00000386/GB/s, CPU at $0.00000772/vCPU/s, volume storage at $0.00000006/GB/s, and egress at $0.05/GB. At full continuous use of the proposed resource caps for 30 days, that is about **$71/month plus egress**; actual usage is metered and may be lower. A **$75/month budget** is the proposed approval boundary, not an enforced billing cap or a quote. Permanent Free's 0.5 GB RAM is below the measured worker footprint; trial capacity is not an ongoing solution.

The measured bundled weights occupy 344,937,328 bytes. A prepared 30-second clip and evidence clips add variable storage; retain at least several GB headroom and monitor disk. There is no automatic evidence deletion. Offline presentation has no hosting fee. An already-owned persistent server is another option; no such server was supplied or verified.

## Prepare a private volume

From the source checkout with local dependencies and permitted assets:

```sh
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python scripts/prepare_online_demo.py \
  --data /absolute/new/private-online-data \
  --origin https://YOUR-ACTUAL-ASSIGNED-HOST \
  --noncommercial-rights-confirmed
```

Use an actual HTTPS origin assigned by the selected provider. Preparation rejects existing directories, verifies local model/media digests, prepares the staged clip, initializes SQLite, and creates one fictional director with a randomly generated password. The password is stored only in `online-access.json` (0600), never printed or placed in the image. No default public password or offline bypass is enabled. Initial camera analysis is disabled.

Transfer the resulting directory privately into `/app/data` on the durable volume, preserving files and changing ownership to container UID/GID 10001. Provisioning/upload and permissions must be verified on the actual provider before launch. Do not put this directory, its access file, an archive of it, or camera source URLs in GitHub or a public image.

Configure the variables in `.env.online.example` with the real HTTPS origin. `MODEL_MANIFEST` is `/app/data/model/manifest.json`; `DORMGUARD_CAMERA_ONLINE` points to `/app/data/test_sources/permitted-demo.mp4`. Leave Supabase and Telegram credentials absent for the isolated demonstration. The service refuses missing models/rights confirmation or an unprovisioned director. Start one replica and verify `/ready`, login, inference, evidence, browser review and restart persistence.

## CI/CD and rollback

GitHub Actions tests source/security contracts, then builds/import-checks the online image. No workflow secret or paid service is created by the workflow. Provider automatic deployment is **not configured or verified**. Before enabling it, restrict deployment to authorized `main` commits after successful checks, provision the durable volume, enter variables privately, and verify the resulting HTTPS service. Code rollback must retain the volume and use a compatible schema; never reset or discard evidence to roll back code.

## Executed local online acceptance

```sh
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python scripts/prepare_online_demo.py \
  --data .online-verified --origin https://127.0.0.1:9443 \
  --noncommercial-rights-confirmed
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python tests/verify_online_runtime.py \
  --workspace .online-verified
```

This opt-in verifier requires fresh prepared data. It starts the actual supervisor and a loopback TLS test proxy, exercises real model-to-incident/evidence/review, runs Chrome at three widths, and restarts the supervisor to verify persisted decisions, evidence, notifications and sessions. The certificate is self-signed and trusted only for this test. This is not a public URL or proof of a cloud deployment. Test processes stop afterward and their private data remains.
