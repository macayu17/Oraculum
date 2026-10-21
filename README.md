# Oraculum

Oraculum is a React study workspace backed by FastAPI. It reads past exam PDFs and page images, compares question patterns, builds a source-linked revision shortlist, and answers follow-up questions using uploaded papers and optional notes. Practice prompts are for revision, not exam predictions.

## Run locally on Windows

Copy `.env.example` to `.env` and set `NVIDIA_API_KEY`. FastAPI loads this ignored file; keep it out of GitHub. The key is never sent to the browser.

Open two PowerShell terminals in the project folder. In the first:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload
```

In the second:

```powershell
cd frontend
npm install
npm run dev
```

Open <http://127.0.0.1:5173>. Vite proxies API requests to FastAPI. For a single production server, run `npm run build` inside `frontend/`, then start FastAPI and open <http://127.0.0.1:8000>. For another machine, create a local `.env` file containing `NVIDIA_API_KEY=your NVIDIA API key`.

## Uploads and limits

- Papers and reference material may be PDFs, PNGs, or JPEGs. Text PDFs are read directly; scans and images are transcribed with the vision model.
- Up to 12 papers and 8 reference files per analysis; each file is limited to 20 MB and each PDF to 40 pages.
- Uploaded contents are sent to NVIDIA's hosted model for analysis and answers. Check extracted questions against the source where scan quality is poor.
- Study sessions and extracted text stay in server memory and are cleared when it restarts. The app has no accounts or persistent document storage.
- Do not upload material you are not allowed to share with that service.

## Checks

Run `python -m unittest discover -s tests -v` in the activated backend environment and `npm run build` inside `frontend/`. The automated checks cover model reasoning parameters, server responsiveness during generation, per-page scan conversion/source labels, and conversation structure. They stub model responses; real NVIDIA inference also needs a live upload check.

## Deploy on Render and Vercel

1. Import this repository as a Render Blueprint using `render.yaml`. Set `NVIDIA_API_KEY` in Render's environment settings. Set `FRONTEND_ORIGINS` to your exact Vercel production origin, such as `https://your-project.vercel.app` (comma-separated if you have additional domains).
2. Import the repository into Vercel with **Root Directory** set to `frontend`. Its `vercel.json` configures the Vite build. Set `VITE_API_BASE_URL` to the Render service URL, with no path, before deploying.
3. Once the Vercel URL is assigned, update Render's `FRONTEND_ORIGINS` to match and redeploy the backend. Changing `VITE_API_BASE_URL` requires rebuilding the frontend.
4. Check `<Render URL>/api/health` reports `ready: true`, then upload papers through the Vercel website and test a follow-up question.

Uploads go directly from the browser to Render, avoiding Vercel function upload limits. Keep one Render instance and one worker: study sessions are in process memory. No database is required. Sessions disappear on backend restarts, redeploys, and free-tier idle shutdowns. Render's free tier may take about a minute to wake after inactivity; it is suitable for a demo, with no paid resources required by this configuration.
