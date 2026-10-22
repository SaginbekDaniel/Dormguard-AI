# DormGuard AI — offline and protected online demo

Current verification (22 October 2026): **offline workflow verified on this Mac; public deployment blocked pending hosting budget approval.** The original application has been imported from the supplied archive. FastAPI serves the existing seven-screen JavaScript dashboard; SQLite, model inference, evidence, alerts and review run locally.

```sh
# Connected setup on a new Mac with Python 3.12; local model/video assets are separate.
./scripts/setup-demo.sh
# These two commands perform no downloads:
./scripts/check-offline-demo.sh --model-rights-confirmed
./scripts/start-demo.sh --model-rights-confirmed
```

Open [http://127.0.0.1:8877](http://127.0.0.1:8877), enter as Demo Director, and start the staged-conflict source. Scores are uncalibrated; human review is required. The VideoMAE demo model is CC BY-NC 4.0 and is restricted to permitted noncommercial demonstrations. It also flags the boxing negative control: no field accuracy claim is made.

- [Demo guide](docs/DEMO_GUIDE.md) and [offline setup](docs/OFFLINE_SETUP.md)
- [Current verification report](docs/FINAL_MVP_REPORT.md) and [AI evaluation](docs/AI_EVALUATION.md)
- [Online deployment and cost gate](docs/ONLINE_DEPLOYMENT.md)

The online image serves frontend/API and supervises the real inference worker. It requires HTTPS, a private credential and one persistent volume containing SQLite, model, sources and evidence. Offline one-click entry is unavailable online. No Supabase credential is needed by either demo mode. Existing production Supabase support remains separate; the historical compromised password must be rotated before that integration is reused.

Historical internal reports, private screenshots and handoffs remain preserved locally in the supplied archive and working folder. They are excluded from public source publication.
