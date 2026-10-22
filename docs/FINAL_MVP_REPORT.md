# Current MVP verification — 22 October 2026

**Offline hackathon workflow: READY in the executed demo scope. Online MVP: PARTIAL, public deployment BLOCKED by unapproved hosting spend. Overall: MVP PARTIAL.** Model field quality remains unvalidated. No public URL exists and none is claimed.

The supplied archive restored the existing Python/FastAPI, plain-JavaScript seven-screen app, migrations, tests, model checkpoints and permitted evaluation films. The 28-file source inventory passed before application work. The repository's existing `main` initial commit `f4d7aa42b2e7bbc71d7984712ab48e632f7478b9` was fetched and preserved as the import parent. The archive's old `.git` snapshots were not substituted for upstream history. No historical passing report was counted as a fresh test.

## Implemented and reviewed

- Reconstructed the local Python 3.12 environment with the supplied pinned packages, installed a pinned local FFmpeg binary, and added explicit connected setup and self-contained offline launch wrappers.
- Fixed startup readiness: a live API no longer causes “Demo ready” before the model/worker/database are ready. Added failure/timeout regressions.
- Fixed screen/incident URL persistence after a real HTTPS browser reproduced loss on refresh. Closing review removes its incident target, and logout clears it. Added a controlled Chrome reload regression; existing CSRF/authorization remain unchanged.
- Added safe private online-volume preparation: no existing-directory overwrite, strict model/media checks, random credential in a 0600 file, fictional director, disabled initial camera, and preserved provenance.
- Consolidated online model, SQLite, test video and protected evidence on one durable volume. CPU-only Docker dependency installation avoids CUDA downloads. The supervisor has an explicit loopback option for local tests and retains container binding by default.
- Added actual local HTTPS/supervisor/model/browser/restart verification, permitted VideoMAE evaluation mode, a provider config file, source-secret scanning and an image-build CI job gated on application/browser tests.
- Excluded model weights, footage, databases, runtime directories, credentials and all 80 historical private screenshots from source publication. Historical text reports, handoffs, local agent instructions and tool-session caches are also retained locally and excluded from public publication.

Principal self-review covered demo-mode isolation, credential handling, session/CSRF invariants, evidence access, volume ownership, child shutdown, URL lifecycle and denial/readiness semantics. No independent-review claim is made. There were no schema/RLS/incident-state changes or known compromised Supabase credential reuse.

## Fresh verification

| Gate | Result and scope |
|---|---|
| Source inventory | PASS, 28 required files; presence only |
| Baseline application regressions | PASS, 140 tests / 20.328 s, disposable local data |
| Final application regressions | PASS, 144 tests / 19.809 s |
| Source-tooling tests | PASS, 7 tests; includes a regression rejecting staged tool-session caches |
| Static/runtime consistency | PASS, Ruff check/format 66 Python files, compileall, JS syntax, pip check |
| Controlled client checks | PASS, 27 Chrome checks including new refresh regression; API doubles, no inference claim |
| Actual offline browser journey | PASS, 13 checks / about 44 s; real Chrome/FastAPI/SQLite/VideoMAE and protected video, all seven screens at 390/768/1440 |
| Network isolation | PASS, application/worker/FFmpeg outbound denied by macOS profile; external socket EPERM, loopback bind allowed; Chrome cross-origin requests aborted and zero external requests observed |
| Actual online-mode browser | PASS, private login and secure cookies, seven screens at three widths, verified playable evidence, refresh; localhost self-signed TLS test only |
| Actual online-mode runtime/restart | PASS / 26.106 s, real supervised worker/inference/SQL, incident/alert, review, evidence SHA-256, anonymous denial, persisted session/decision/evidence/notification after restart |
| Model comparison | PASS execution on three permitted films; VideoMAE detects staged positive, greeting negative passes, boxing false alert; ViT misses staged positive and also false-alerts on boxing |
| Hosting configuration | PASS against Railway's live JSON schema; pinned official checkout/setup-python Git tags verified |
| Secret scan | PASS on nonignored source; staged scan also passes on the narrowed publication set. Heuristic scan does not prove absence of every unknown secret format |
| Docker image build/run | NOT_RUN locally: no Docker runtime. GitHub image-build job is configured; actual run status must be checked separately |
| Public HTTPS deployment/E2E | BLOCKED: no paid resource authorization, no public service/domain provisioned |
| Provider CI/CD integration | NOT_CONFIGURED: GitHub push does not itself deploy this application |

The first offline browser attempt found an asynchronous test assertion that read the notification counter before the mutation completed. It was corrected to wait for persistence. The first online browser run reproduced the actual incident-refresh bug; the source fix and subsequent controlled/local-HTTPS checks passed. A later offline assertion was updated to expect the now-preserved Live Cameras screen on refresh. Earlier failed runs and their evidence are retained privately, never overwritten into a claimed pass.

Commands include `python tools/check_source_readiness.py`, `.venv/bin/python -m unittest discover -s tests -v`, `.venv/bin/python -m unittest discover -s tools/tests -v`, `.venv/bin/python -m ruff check dormguard scripts tests`, `.venv/bin/python -m ruff format --check dormguard scripts tests`, `node --check dormguard/app.js`, `.venv/bin/python -m pip check`, `DORMGUARD_BROWSER_CHANNEL=chrome node tests/frontend_browser.cjs`, the OS-isolated launch plus `node tests/offline_browser.cjs`, and the exact online/evaluation commands linked below. No real Telegram messages or physical camera access occurred.

Private local verification artifacts (excluded from GitHub): `docs/engineering/OFFLINE_BROWSER_RESULTS.json`, `ONLINE_BROWSER_RESULTS.json`, `ONLINE_RUNTIME_RESULTS.json`, `OFFLINE_VIDEOMAE_RESULTS.json`, `OFFLINE_VIT_RESULTS.json`. Screenshots and raw service/session logs remain private; current screenshots are under `/private/tmp/dormguard-offline-screenshots/` and `/private/tmp/dormguard-online-evidence.png` on this Mac.

## Architecture, costs and remaining user action

The smallest proposed persistent topology is one CPU container, one private volume, one replica and provider HTTPS. Proposed initial resources: 2 vCPU, 3 GB RAM, 5 GB storage. Measured model/worker footprint exceeds the permanent Railway Free service memory allowance. Current metered rates imply roughly $71/month plus egress if those caps are continuously consumed. Approval of a **$75/month hosting budget**, or an already-owned suitable server, is needed before provisioning. This is an estimate, not an enforced account cap. See [online deployment and rate source](ONLINE_DEPLOYMENT.md).

GitHub destination is `https://github.com/SaginbekDaniel/Dormguard-AI`; publication is authorized by the original request and supplied repository. Final commit/push and CI results are recorded in the delivery message and repository history after the staged scan. This document does not claim a completed cloud deployment or provider integration.

No Supabase credential is required for these isolated demos. Reusing the historical hosted integration remains blocked until its owner rotates the compromised password and updates/verifies consumers. Real Telegram delivery additionally needs bot configuration, consenting recipients and explicit send scope. Physical camera testing, representative held-out quality, MPS/CUDA comparison, institutional rollout, backups/retention/legal holds, load/soak and production assurance remain unexecuted separate scopes.

Launch the local demonstration with `./scripts/start-demo.sh --model-rights-confirmed`. See [presentation sequence](DEMO_GUIDE.md), [offline setup](OFFLINE_SETUP.md), [AI results](AI_EVALUATION.md) and [online preparation](ONLINE_DEPLOYMENT.md).
