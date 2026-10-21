import base64
import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Literal

import pymupdf as fitz
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError


ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
MODELS = ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite")
MODEL = MODELS[0]
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PAGES = 40
SCAN_BATCH_SIZE = 3
threads: dict[str, dict] = {}
analysis_jobs: dict[str, dict] = {}
analysis_tasks: set[asyncio.Task] = set()
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


class PaperExtraction(BaseModel):
    model_config = {"extra": "forbid"}
    questions: list[PaperQuestion]


class QuestionClassification(BaseModel):
    model_config = {"extra": "forbid"}
    index: int
    repeat: Literal["exact", "reworded", "topic", "new"]


class ComparisonReport(BaseModel):
    model_config = {"extra": "forbid"}
    summary: str
    classifications: list[QuestionClassification]
    patterns: list[str]
    practice: list[str]


class ClassificationRepair(BaseModel):
    model_config = {"extra": "forbid"}
    classifications: list[QuestionClassification]


@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY is not set. Add your Gemini API key to the backend environment and restart Oraculum.",
        )
    return genai.Client(api_key=key)


def gemini_parts(messages: list[dict]) -> tuple[list, str | None]:
    contents, system = [], []
    for message in messages:
        role = message["role"]
        content = message.get("content", "")
        if role == "system":
            system.append(content if isinstance(content, str) else " ".join(item.get("text", "") for item in content if item.get("type") == "text"))
            continue
        parts = []
        for item in content if isinstance(content, list) else [{"type": "text", "text": content}]:
            if item.get("type") == "text":
                parts.append(types.Part.from_text(text=item["text"]))
            elif item.get("type") == "image_url":
                url = item["image_url"]["url"]
                header, data = url.split(",", 1)
                parts.append(types.Part.from_bytes(data=base64.b64decode(data), mime_type=header.split(";")[0][5:]))
            elif item.get("type") == "file_data":
                file = item["file_data"]
                parts.append(types.Part.from_bytes(data=file["data"], mime_type=file["mime_type"]))
        contents.append(types.Content(role="model" if role == "assistant" else "user", parts=parts))
    return contents, "\n\n".join(part for part in system if part) or None


def answer(messages: list[dict], *, max_tokens: int = 6000, reasoning_effort: str = "low", response_format: dict | None = None) -> str:
    try:
        contents, system_instruction = gemini_parts(messages)
        schema = response_format.get("json_schema", {}).get("schema") if response_format else None
        client = get_client()
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.6,
            top_p=0.95,
            max_output_tokens=max_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=reasoning_effort.upper()),
            response_mime_type="application/json" if schema else None,
            response_json_schema=schema,
        )
        for index, model in enumerate(MODELS):
            try:
                result = client.models.generate_content(model=model, contents=contents, config=config)
                break
            except Exception as exc:
                if index < len(MODELS) - 1 and getattr(exc, "code", None) in {429, 500, 502, 503, 504}:
                    logger.warning("Gemini model %s unavailable (%s); retrying with %s", model, exc.code, MODELS[index + 1])
                    continue
                raise
        if result.candidates and result.candidates[0].finish_reason == types.FinishReason.MAX_TOKENS:
            raise HTTPException(status_code=502, detail="The model reached its response limit before finishing. Compare fewer papers in one analysis.")
        content = result.text
        if not content or not content.strip():
            raise HTTPException(status_code=502, detail="Gemini returned an empty answer. Please retry.")
        return content
    except HTTPException:
        raise
    except Exception as exc:
        if getattr(exc, "code", None) == 429 or getattr(exc, "code", 0) >= 500:
            raise HTTPException(status_code=503, detail="Gemini is temporarily overloaded on both available models. Please retry in a minute.") from exc
        if "timed out" in str(exc).casefold() or "timeout" in str(exc).casefold():
            raise HTTPException(status_code=504, detail="Gemini took too long to answer. Try fewer papers or split long papers into smaller files.") from exc
        raise HTTPException(status_code=502, detail=f"Gemini request failed: {exc}") from exc


async def read_document(upload: UploadFile, progress=None) -> str:
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
        transcription = await run_in_threadpool(answer, [{"role": "user", "content": [{"type": "text", "text": f"Transcribe this exam or study-material image faithfully. Preserve question numbering, equations, choices, and marks. Mark illegible text as [unclear]. Begin with the source label [{filename}, page 1]. Return plain text only."}, *media]}], max_tokens=12000, reasoning_effort="low")
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

    pages: list[str | None] = [None] * len(pdf)
    scan_batch = []

    async def transcribe_batch(batch):
        texts = await asyncio.gather(*(
            run_in_threadpool(
                answer,
                [{"role": "user", "content": [
                    {"type": "text", "text": "Transcribe this page faithfully. Preserve question numbering, equations, tables, choices, and marks. Mark illegible text as [unclear]. Return the page text only."},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")}},
                ]}],
                max_tokens=6000,
                reasoning_effort="low",
            )
            for _, _, image in batch
        ))
        for (index, source, _), text in zip(batch, texts):
            pages[index] = f"{source}\n{text}"
            logger.info("Transcribed %s", source)
            if progress:
                progress(f"Reading {filename}: page {index + 1} of {len(pdf)}")

    try:
        for index, page in enumerate(pdf):
            number = index + 1
            source = f"[{filename}, page {number}]"
            text = page.get_text().strip()
            if not text:
                scale = min(2.0, 2000 / max(page.rect.width, page.rect.height))
                image = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("jpeg", jpg_quality=85)
                logger.info("Transcribing %s page %s of %s", filename, number, len(pdf))
                scan_batch.append((index, source, image))
                if len(scan_batch) == SCAN_BATCH_SIZE:
                    await transcribe_batch(scan_batch)
                    scan_batch = []
            else:
                pages[index] = f"{source}\n{text}"
                if progress:
                    progress(f"Reading {filename}: page {number} of {len(pdf)}")
        if scan_batch:
            await transcribe_batch(scan_batch)
    finally:
        pdf.close()

    content = "\n\n".join(page for page in pages if page).strip()
    if not content:
        raise HTTPException(status_code=400, detail=f"No readable text found in {filename}.")
    return content


async def extract_paper(upload: UploadFile, progress=None) -> list[PaperQuestion]:
    filename = upload.filename or "upload"
    suffix = Path(filename).suffix.lower()
    mime_type = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(suffix)
    if not mime_type:
        raise HTTPException(status_code=400, detail="Upload PDF, PNG, or JPEG files only.")
    raw = await upload.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"{filename} exceeds the 20 MB limit.")
    if suffix == ".pdf":
        try:
            pdf = fitz.open(stream=raw, filetype="pdf")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not read {filename} as a PDF.") from exc
        try:
            if pdf.is_encrypted:
                raise HTTPException(status_code=400, detail=f"{filename} is password-protected.")
            if len(pdf) > MAX_PAGES:
                raise HTTPException(status_code=413, detail=f"{filename} has more than {MAX_PAGES} pages.")
            page_count = len(pdf)
        finally:
            pdf.close()
        if progress:
            progress(f"Sending {filename} ({page_count} pages) to Gemini for analysis")
    prompt = f"""Read this exam paper and extract every readable question and subquestion. Keep alternatives distinct and preserve exact wording, numbering, marks, and page numbers. Set paper to the filename {filename!r}. Set repeat to new for now; recurrence is compared across papers afterward. Do not invent unreadable content. Return only the structured questions."""
    raw_response = await run_in_threadpool(
        answer,
        [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "file_data", "file_data": {"data": raw, "mime_type": mime_type}},
        ]}],
        max_tokens=12000, reasoning_effort="low",
        response_format={"type": "json_schema", "json_schema": {"name": "paper_extraction", "strict": True, "schema": PaperExtraction.model_json_schema()}},
    )
    try:
        questions = PaperExtraction.model_validate(parse_json(raw_response)).questions
    except ValidationError as exc:
        raise HTTPException(status_code=502, detail=f"Gemini could not extract questions from {filename}. Please retry with a clearer paper.") from exc
    if progress:
        progress(f"Extracted {len(questions)} questions from {filename}")
    return questions


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


async def stage_upload(upload: UploadFile) -> tuple[str, str]:
    filename = upload.filename or "upload"
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".png", ".jpg", ".jpeg"}:
        raise HTTPException(status_code=400, detail="Upload PDF, PNG, or JPEG files only.")
    fd, staged_path = tempfile.mkstemp(suffix=suffix, prefix="oraculum-")
    size = 0
    try:
        with os.fdopen(fd, "wb") as target:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise HTTPException(status_code=413, detail=f"{filename} exceeds the 20 MB limit.")
                target.write(chunk)
        return filename, staged_path
    except Exception:
        Path(staged_path).unlink(missing_ok=True)
        raise


def prune_analysis_jobs() -> None:
    expired = [
        job_id for job_id, job in analysis_jobs.items()
        if job["status"] != "processing" and time.monotonic() - job["updated"] > 3600
    ]
    for job_id in expired:
        del analysis_jobs[job_id]


async def compare_documents(papers: list[UploadFile], materials: list[UploadFile], syllabus: str, progress=None) -> dict:
    extracted, notes = [], []
    for index, upload in enumerate(papers, 1):
        if progress:
            progress(f"Reading paper {index} of {len(papers)}: {upload.filename}")
        logger.info("Analyzing paper: %s", upload.filename)
        extracted.extend(await extract_paper(upload, progress))
    for index, upload in enumerate(materials, 1):
        if progress:
            progress(f"Reading reference {index} of {len(materials)}: {upload.filename}")
        text = await read_document(upload, progress)
        notes.append(text[:60000])

    if progress:
        progress("Comparing questions and recurring topics across papers")
    logger.info("Comparing %s extracted questions from %s papers", len(extracted), len(papers))
    prompt = """Compare these numbered questions from past exam papers for a student preparing to revise. Treat the supplied questions, syllabus, and references as the only evidence. Do not claim to know or guarantee future exam questions. Return one JSON object with summary (string), classifications (array of objects with index and repeat), patterns (array of strings), and practice (array of strings). Return exactly one classification for every question index, without repeating the question text. The index is the zero-based array position: the first supplied question is index 0, the second is index 1, and so on. Set repeat to exact only for the same question, reworded for the same ask with changed wording, topic for a recurring concept asked differently, and new when it appears once. Patterns should describe observed structure and coverage. Practice should contain plausible new practice questions based on recurring concepts, labelled as practice, never as predictions with probabilities. Use valid JSON only."""
    context = "\n\nSyllabus:\n" + (syllabus[:12000] or "Not provided.")
    if notes:
        context += "\n\nReference material (context only):\n" + "\n\n".join(notes)[:12000]
    question_data = json.dumps([question.model_dump() for question in extracted], ensure_ascii=False, separators=(",", ":"))
    raw = await run_in_threadpool(
        answer, [{"role": "user", "content": [{"type": "text", "text": prompt + context + "\n\nExtracted questions:\n" + question_data}]}],
        max_tokens=4000, reasoning_effort="low",
        response_format={"type": "json_schema", "json_schema": {"name": "question_comparison", "strict": True, "schema": ComparisonReport.model_json_schema()}},
    )
    try:
        comparison = ComparisonReport.model_validate(parse_json(raw))
    except ValidationError as exc:
        raise HTTPException(status_code=502, detail="The model returned an incomplete report. Please retry.") from exc
    classifications = {item.index: item.repeat for item in comparison.classifications}
    if set(classifications) == set(range(1, len(extracted) + 1)):
        classifications = {index - 1: repeat for index, repeat in classifications.items()}
    missing = sorted(set(range(len(extracted))) - set(classifications))
    if missing:
        logger.warning("Retrying classification for %s omitted questions", len(missing))
        repair_prompt = """Classify only the requested zero-based question indexes. Compare each against the full supplied question list and use exact, reworded, topic, or new. Return exactly one classification for each requested index, with no summary, patterns, or practice."""
        repair_raw = await run_in_threadpool(
            answer,
            [{"role": "user", "content": [{"type": "text", "text": repair_prompt + "\nRequested indexes: " + json.dumps(missing) + "\n\nAll extracted questions:\n" + question_data}]}],
            max_tokens=min(4000, max(500, len(missing) * 50)), reasoning_effort="low",
            response_format={"type": "json_schema", "json_schema": {"name": "classification_repair", "strict": True, "schema": ClassificationRepair.model_json_schema()}},
        )
        try:
            repaired = ClassificationRepair.model_validate(parse_json(repair_raw)).classifications
        except ValidationError as exc:
            raise HTTPException(status_code=502, detail="The model could not complete question classification. Please retry.") from exc
        repaired_map = {item.index: item.repeat for item in repaired}
        if set(repaired_map) == {index + 1 for index in missing}:
            repaired_map = {index - 1: repeat for index, repeat in repaired_map.items()}
        classifications.update({index: repeat for index, repeat in repaired_map.items() if index in missing})
    if set(classifications) != set(range(len(extracted))):
        raise HTTPException(status_code=502, detail="The model did not classify every extracted question. Please retry.")
    questions = [question.model_copy(update={"repeat": classifications[index]}).model_dump() for index, question in enumerate(extracted)]
    if not questions:
        raise HTTPException(status_code=502, detail="No readable questions were returned. Check the scan and try again.")
    topics = group_topics(questions)
    thread_id = str(uuid.uuid4())
    threads[thread_id] = {"context": ("Extracted paper questions:\n" + question_data + "\n\nReference material:\n" + "\n\n".join(notes))[:250000], "messages": []}
    while len(threads) > 30:
        threads.pop(next(iter(threads)))
    return {
        "thread_id": thread_id,
        "summary": comparison.summary,
        "questions": questions,
        "topics": topics,
        "patterns": comparison.patterns,
        "practice": comparison.practice,
        "paper_count": len(papers),
        "material_count": len(materials),
    }


async def process_analysis(job_id: str, staged_papers: list[tuple[str, str]], staged_materials: list[tuple[str, str]], syllabus: str) -> None:
    uploads = []
    try:
        papers = [UploadFile(filename=name, file=Path(path).open("rb")) for name, path in staged_papers]
        materials = [UploadFile(filename=name, file=Path(path).open("rb")) for name, path in staged_materials]
        uploads = papers + materials
        result = await compare_documents(papers, materials, syllabus, lambda message: analysis_jobs[job_id].update(progress=message, updated=time.monotonic()))
        analysis_jobs[job_id].update(status="complete", result=result, updated=time.monotonic())
    except HTTPException as exc:
        analysis_jobs[job_id].update(status="failed", detail=exc.detail, updated=time.monotonic())
    except Exception:
        logger.exception("Analysis job %s failed", job_id)
        analysis_jobs[job_id].update(status="failed", detail="Analysis failed unexpectedly. Please try again.", updated=time.monotonic())
    finally:
        for upload in uploads:
            await upload.close()
        for _, path in staged_papers + staged_materials:
            Path(path).unlink(missing_ok=True)



@app.get("/api/health")
def health():
    return {"ready": bool(os.getenv("GEMINI_API_KEY")), "model": MODEL, "fallback_models": MODELS[1:]}


@app.post("/api/analyze", status_code=202)
async def analyze(
    papers: list[UploadFile] = File(...),
    materials: list[UploadFile] = File(default=[]),
    syllabus: str = Form(default=""),
):
    if not papers:
        raise HTTPException(status_code=400, detail="Upload at least one question paper.")
    if len(papers) > 12 or len(materials) > 8:
        raise HTTPException(status_code=413, detail="Upload at most 12 papers and 8 reference files at once.")
    prune_analysis_jobs()
    if len(analysis_jobs) >= 50:
        raise HTTPException(status_code=503, detail="The analysis queue is full. Try again shortly.")

    staged_papers, staged_materials = [], []
    try:
        for upload in papers:
            staged_papers.append(await stage_upload(upload))
        for upload in materials:
            staged_materials.append(await stage_upload(upload))
    except Exception:
        for _, path in staged_papers + staged_materials:
            Path(path).unlink(missing_ok=True)
        raise

    job_id = str(uuid.uuid4())
    analysis_jobs[job_id] = {"status": "processing", "progress": "Preparing files", "updated": time.monotonic()}
    task = asyncio.create_task(process_analysis(job_id, staged_papers, staged_materials, syllabus))
    analysis_tasks.add(task)
    task.add_done_callback(analysis_tasks.discard)
    return {"job_id": job_id, "status": "processing"}


@app.get("/api/analyze/{job_id}")
def analysis_status(job_id: str):
    prune_analysis_jobs()
    job = analysis_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="This analysis expired or was not found. Upload the papers again to start a new analysis.")
    if job["status"] == "complete":
        return {"status": "complete", "result": job["result"]}
    if job["status"] == "failed":
        return {"status": "failed", "detail": job["detail"]}
    return {"status": "processing", "progress": job.get("progress", "Analyzing papers")}


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
    reply = answer(messages, max_tokens=3000, reasoning_effort="low")
    thread["messages"].extend([{"role": "user", "content": request.message.strip()}, {"role": "assistant", "content": reply}])
    return {"reply": reply}

FRONTEND_DIST = ROOT / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    @app.get("/")
    def frontend_not_built():
        return HTMLResponse("Build the React site with npm install and npm run build in frontend/.", status_code=503)
