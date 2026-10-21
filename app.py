import base64
import json
import logging
import os
import re
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Literal

import pymupdf as fitz
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from dotenv import load_dotenv
from openai import NOT_GIVEN, OpenAI
from pydantic import BaseModel, ValidationError


ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"
BASE_URL = "https://integrate.api.nvidia.com/v1"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PAGES = 40
threads: dict[str, dict] = {}
logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Oraculum", docs_url=None, redoc_url=None)
frontend_origins = [origin.strip().rstrip("/") for origin in os.getenv("FRONTEND_ORIGINS", "").split(",") if origin.strip()]
if frontend_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=frontend_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )


class ChatRequest(BaseModel):
    thread_id: str
    message: str


class PaperQuestion(BaseModel):
    model_config = {"extra": "forbid"}
    paper: str
    page: str | int | None
    question: str
    concept: str
    marks: int | None
    type: str
    repeat: Literal["exact", "reworded", "topic", "new"]


class AnalysisReport(BaseModel):
    model_config = {"extra": "forbid"}
    summary: str
    questions: list[PaperQuestion]
    patterns: list[str]
    practice: list[str]


def get_client() -> OpenAI:
    key = os.getenv("NVIDIA_API_KEY")
    if not key:
        raise HTTPException(
            status_code=503,
            detail="NVIDIA_API_KEY is not set. Add your NVIDIA API key to the environment and restart Oraculum.",
        )
    return OpenAI(api_key=key, base_url=BASE_URL, timeout=180, max_retries=0)


def answer(messages: list[dict], *, max_tokens: int = 6000, reasoning_effort: str = "none", response_format: dict | None = None) -> str:
    try:
        result = get_client().chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.6,
            top_p=0.95,
            max_tokens=max_tokens,
            stream=False,
            reasoning_effort=reasoning_effort,
            response_format=response_format or NOT_GIVEN,
        )
        if result.choices[0].finish_reason == "length":
            raise HTTPException(status_code=502, detail="The model reached its response limit before finishing. Compare fewer papers in one analysis.")
        content = result.choices[0].message.content
        if not content or not content.strip():
            raise HTTPException(status_code=502, detail="NVIDIA returned an empty answer. Please retry.")
        return content
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"NVIDIA request failed: {exc}") from exc


async def read_document(upload: UploadFile) -> str:
    filename = upload.filename or "upload"
    suffix = Path(filename).suffix.lower()
    image_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    if suffix not in image_types and suffix != ".pdf":
        raise HTTPException(status_code=400, detail="Upload PDF, PNG, or JPEG files only.")
    raw = await upload.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"{filename} exceeds the 20 MB limit.")
    if suffix in image_types:
        media = [{"type": "image_url", "image_url": {"url": f"data:{image_types[suffix]};base64," + base64.b64encode(raw).decode("ascii")}}]
        transcription = await run_in_threadpool(answer, [{"role": "user", "content": [{"type": "text", "text": f"Transcribe this exam or study-material image faithfully. Preserve question numbering, equations, choices, and marks. Mark illegible text as [unclear]. Begin with the source label [{filename}, page 1]. Return plain text only."}, *media]}], max_tokens=12000, reasoning_effort="none")
        if not transcription.strip():
            raise HTTPException(status_code=400, detail=f"No readable text found in {filename}.")
        return f"[{filename}, page 1]\n{transcription}"
    try:
        pdf = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read {filename} as a PDF.") from exc
    if pdf.is_encrypted:
        raise HTTPException(status_code=400, detail=f"{filename} is password-protected.")
    if len(pdf) > MAX_PAGES:
        raise HTTPException(status_code=413, detail=f"{filename} has more than {MAX_PAGES} pages.")

    pages = []
    try:
        for number, page in enumerate(pdf, 1):
            source = f"[{filename}, page {number}]"
            text = page.get_text().strip()
            if not text:
                scale = min(2.0, 2000 / max(page.rect.width, page.rect.height))
                image = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("jpeg", jpg_quality=85)
                logger.info("Transcribing %s page %s of %s", filename, number, len(pdf))
                text = await run_in_threadpool(
                    answer,
                    [{"role": "user", "content": [
                        {"type": "text", "text": "Transcribe this page faithfully. Preserve question numbering, equations, tables, choices, and marks. Mark illegible text as [unclear]. Return the page text only."},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")}},
                    ]}],
                    max_tokens=6000,
                    reasoning_effort="none",
                )
            pages.append(f"{source}\n{text}")
    finally:
        pdf.close()

    content = "\n\n".join(pages).strip()
    if not content:
        raise HTTPException(status_code=400, detail=f"No readable text found in {filename}.")
    return content


def parse_json(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="The model returned an unreadable analysis. Please retry with clearer papers.") from exc


def group_topics(questions: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for question in questions:
        if not isinstance(question, dict):
            continue
        key = re.sub(r"\W+", " ", str(question.get("concept", "")).casefold()).strip()
        if key:
            groups[key].append(question)
    topics = []
    for key, items in groups.items():
        papers = sorted({str(item.get("paper", "Paper")) for item in items})
        topics.append({
            "concept": str(items[0].get("concept", key)).strip(),
            "count": len(items),
            "papers": papers,
            "questions": items,
            "marks": sum(int(item["marks"]) for item in items if str(item.get("marks") or "").isdigit()),
            "priority": "High" if len(papers) >= 3 else "Review" if len(papers) == 2 else "Once",
        })
    return sorted(topics, key=lambda item: (len(item["papers"]), item["count"], item["marks"]), reverse=True)



@app.get("/api/health")
def health():
    return {"ready": bool(os.getenv("NVIDIA_API_KEY")), "model": MODEL}


@app.post("/api/analyze")
async def analyze(
    papers: list[UploadFile] = File(...),
    materials: list[UploadFile] = File(default=[]),
    syllabus: str = Form(default=""),
):
    if not papers:
        raise HTTPException(status_code=400, detail="Upload at least one question paper.")
    if len(papers) > 12 or len(materials) > 8:
        raise HTTPException(status_code=413, detail="Upload at most 12 papers and 8 reference files at once.")

    paper_texts, notes = [], []
    for upload in papers:
        logger.info("Reading paper: %s", upload.filename)
        text = await read_document(upload)
        logger.info("Extracted %s characters from %s", len(text), upload.filename)
        paper_texts.append(text[:90000])
    for upload in materials:
        text = await read_document(upload)
        notes.append(text[:60000])

    prompt = """Analyse these past exam papers for a student preparing to revise. Treat the supplied papers and syllabus as the only evidence. Do not claim to know or guarantee future exam questions. Return a single JSON object with keys: summary (string), questions (array), patterns (array of strings), practice (array of strings). Each question must have paper (filename), page (string or number if identifiable), question (faithful text), concept (short normalized topic), marks (integer or null), type (short label), and repeat (one of exact, reworded, topic, new). Include every readable question and subquestion; keep alternatives distinct. Use repeat=exact only for the same question, reworded for the same ask with changed wording, topic for a recurring concept asked differently, and new when it appears once. Patterns should describe observed structure and coverage. Practice should contain plausible new practice questions based on recurring concepts, labelled as practice, never as predictions with probabilities. Use valid JSON only."""
    user_content = [{"type": "text", "text": prompt + "\n\nSyllabus:\n" + (syllabus[:12000] or "Not provided.") + "\n\nPast papers:\n" + "\n\n".join(paper_texts)}]
    logger.info("Comparing %s extracted papers", len(paper_texts))
    raw = await run_in_threadpool(
        answer, [{"role": "user", "content": user_content}],
        max_tokens=12000, reasoning_effort="none",
        response_format={"type": "json_schema", "json_schema": {"name": "paper_analysis", "strict": True, "schema": AnalysisReport.model_json_schema()}},
    )
    try:
        report = AnalysisReport.model_validate(parse_json(raw)).model_dump()
    except ValidationError as exc:
        raise HTTPException(status_code=502, detail="The model returned an incomplete report. Please retry.") from exc
    questions = report.get("questions")
    if not isinstance(questions, list):
        raise HTTPException(status_code=502, detail="The model analysis did not include a question list. Please retry.")
    questions = [question for question in questions if isinstance(question, dict)]
    if not questions:
        raise HTTPException(status_code=502, detail="No readable questions were returned. Check the scan and try again.")
    topics = group_topics(questions)
    thread_id = str(uuid.uuid4())
    threads[thread_id] = {"context": "\n\n".join(paper_texts + notes)[:250000], "messages": []}
    while len(threads) > 30:
        threads.pop(next(iter(threads)))
    return {
        "thread_id": thread_id,
        "summary": str(report.get("summary", "")),
        "questions": questions,
        "topics": topics,
        "patterns": report.get("patterns", []),
        "practice": report.get("practice", []),
        "paper_count": len(papers),
        "material_count": len(materials),
    }


@app.post("/api/chat")
def chat(request: ChatRequest):
    thread = threads.get(request.thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="This study session expired. Analyse the papers again to start a new one.")
    if not request.message.strip() or len(request.message) > 4000:
        raise HTTPException(status_code=400, detail="Enter a question up to 4,000 characters.")
    messages = [
        {"role": "system", "content": "You are Oraculum, a careful exam revision tutor. Answer from the supplied papers and notes. Point to source filenames and page numbers when available. If the material does not support an answer, say so and ask for the relevant textbook or notes. Separate observed past-paper patterns from generated practice. Never promise a question will appear."},
        {"role": "user", "content": "Uploaded paper and reference material:\n" + thread["context"]},
        {"role": "assistant", "content": "I will use these papers and references to answer your revision questions."},
        *thread["messages"][-10:],
        {"role": "user", "content": request.message.strip()},
    ]
    reply = answer(messages, max_tokens=3000, reasoning_effort="none")
    thread["messages"].extend([{"role": "user", "content": request.message.strip()}, {"role": "assistant", "content": reply}])
    return {"reply": reply}

FRONTEND_DIST = ROOT / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    @app.get("/")
    def frontend_not_built():
        return HTMLResponse("Build the React site with npm install and npm run build in frontend/.", status_code=503)
