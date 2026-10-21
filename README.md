# Oraculum

Oraculum helps students review past exam papers. It extracts questions from PDFs and images, compares topics and question patterns, and builds a revision list with references to the source papers. Students can also ask follow-up questions using their uploaded papers and notes.

Practice questions are study prompts based on the supplied material. They are not predictions of future exam questions.

**Website:** [oraculum-one.vercel.app](https://oraculum-one.vercel.app) | **Backend health:** [oraculum-api-og1a.onrender.com/api/health](https://oraculum-api-og1a.onrender.com/api/health)

## Features

- Analyze text-based and scanned exam papers.
- Compare questions, recurring topics, marks, and paper structure.
- Review source-linked questions, patterns, and practice prompts.
- Ask a study assistant questions grounded in the uploaded papers and notes.
- Upload PDF, PNG, and JPEG files.

## Technology

- React and Vite frontend
- FastAPI backend
- Gemini 3.8 Flash, with Gemini 3.6 Flash as an overload fallback
- Google Gen AI Python SDK and PyMuPDF for native PDF input and validation

## Run locally

Requirements: Python 3.13 or later and Node.js.

Create a project-root `.env` file from the example, then add your Gemini API key:

```powershell
Copy-Item .env.example .env
```

The backend reads `GEMINI_API_KEY` from `.env`. Keep the key out of source control and frontend environment variables.

In one PowerShell terminal, install and start the backend:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload
```

In a second terminal, start the frontend:

```powershell
cd frontend
npm install
npm run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). During local development, Vite proxies API requests to the backend on port 8000.

To serve the built frontend from FastAPI, build it first:

```powershell
cd frontend
npm run build
cd ..
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Upload limits and data handling

- Each file can be up to 20 MB. PDFs can contain up to 40 pages.
- Each analysis accepts up to 12 exam papers and 8 reference files.
- Question papers are sent to Gemini as PDF or image input for question extraction and analysis. Reference material may be converted to text first.
- Uploaded text and study sessions are held in backend memory. They are cleared when the backend restarts. The application does not use accounts or persistent document storage.
- Temporary Gemini capacity errors fall back from Gemini 3.8 Flash to Gemini 3.6 Flash. Other request failures are shown so the analysis can be retried.

Only upload material you are permitted to share with Google's hosted Gemini API. Free-tier prompts and responses may be used to improve Google's products; review the current API terms before uploading sensitive material.

## Deploy

The frontend is hosted on Vercel and the API is hosted on Render. The project is configured for deployment from the GitHub repository.

### Render backend

The root-level `render.yaml` configures a Python web service. Set these environment variables in Render:

- `GEMINI_API_KEY`: your Gemini API key from Google AI Studio.
- `FRONTEND_ORIGINS`: the exact Vercel website origin, such as `https://your-project.vercel.app`.

For additional domains, list their origins separated by commas. The deployed backend is available at [https://oraculum-api-og1a.onrender.com](https://oraculum-api-og1a.onrender.com).

### Vercel frontend

Import the repository into Vercel and set the project root directory to `frontend`. The `frontend/vercel.json` file configures the Vite build. Set `VITE_API_BASE_URL` to the Render backend URL, without a path, before building. This value is public frontend configuration; never put secrets in variables prefixed with `VITE_`.

Uploads are sent directly from the browser to Render. After changing either service's environment variables, redeploy the relevant service. The frontend's production origin must match `FRONTEND_ORIGINS` on Render.

## Verification

Run the backend unit tests from the repository root:

```powershell
python -m unittest discover -s tests -v
```

Build the frontend:

```powershell
cd frontend
npm run build
```

The unit tests use stubbed model responses. Testing real analysis requires a valid Gemini API key and an uploaded document.

## Deployment notes

The Render service uses one instance and one worker because study sessions are stored in process memory. No database is required for the current application. Sessions are lost when the service restarts, redeploys, or shuts down while idle. Render's free service may take about a minute to start after inactivity; this plan is intended for demonstrations and evaluation.
