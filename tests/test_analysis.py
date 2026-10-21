import asyncio
import base64
from io import BytesIO
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pymupdf

import app


class ModelRequestTests(unittest.TestCase):
    def test_reasoning_mode_and_json_schema_are_sent_to_gemini(self):
        generate = Mock(return_value=SimpleNamespace(candidates=[], text="A reply"))
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        with patch.object(app, "get_client", return_value=client):
            self.assertEqual(app.answer([], reasoning_effort="low", response_format={"json_schema": {"schema": {"type": "object"}}}), "A reply")
        self.assertEqual(generate.call_args.kwargs["model"], "gemini-3.8-flash")
        self.assertEqual(generate.call_args.kwargs["config"].thinking_config.thinking_level, "LOW")
        self.assertEqual(generate.call_args.kwargs["config"].response_json_schema, {"type": "object"})

    def test_native_pdf_input_is_passed_to_gemini(self):
        contents, system = app.gemini_parts([
            {"role": "system", "content": "Stay grounded in the paper."},
            {"role": "user", "content": [{"type": "text", "text": "Read this."}, {"type": "file_data", "file_data": {"data": b"%PDF", "mime_type": "application/pdf"}}]},
        ])
        self.assertEqual(system, "Stay grounded in the paper.")
        self.assertEqual(contents[0].parts[1].inline_data.mime_type, "application/pdf")
        self.assertEqual(contents[0].parts[1].inline_data.data, b"%PDF")

    def test_falls_back_when_gemini_primary_is_overloaded(self):
        class Overloaded(Exception):
            code = 503

        generate = Mock(side_effect=[Overloaded(), SimpleNamespace(candidates=[], text="A reply")])
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        with patch.object(app, "get_client", return_value=client):
            self.assertEqual(app.answer([], reasoning_effort="low"), "A reply")
        self.assertEqual([call.kwargs["model"] for call in generate.call_args_list], list(app.MODELS[:2]))

    def test_falls_through_to_flash_lite_after_two_models_are_overloaded(self):
        class Overloaded(Exception):
            code = 503

        generate = Mock(side_effect=[Overloaded(), Overloaded(), SimpleNamespace(candidates=[], text="A reply")])
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        with patch.object(app, "get_client", return_value=client):
            self.assertEqual(app.answer([]), "A reply")
        self.assertEqual([call.kwargs["model"] for call in generate.call_args_list], list(app.MODELS))

    def test_reports_a_retryable_error_when_both_models_are_overloaded(self):
        class Overloaded(Exception):
            code = 503

        client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=Overloaded())))
        with patch.object(app, "get_client", return_value=client):
            with self.assertRaises(app.HTTPException) as error:
                app.answer([])
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(client.models.generate_content.call_count, len(app.MODELS))
        self.assertIn("temporarily overloaded", error.exception.detail)


    def test_truncated_completion_is_not_returned_as_a_report(self):
        generate = Mock(return_value=SimpleNamespace(candidates=[SimpleNamespace(finish_reason=app.types.FinishReason.MAX_TOKENS)], text="partial"))
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        with patch.object(app, "get_client", return_value=client):
            with self.assertRaises(app.HTTPException) as error:
                app.answer([])
        self.assertEqual(error.exception.status_code, 502)
        self.assertIn("response limit", error.exception.detail)


class UploadResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_scanned_pdf_transcribes_pages_concurrently_in_source_order(self):
        document = pymupdf.open()
        for _ in range(4):
            document.new_page()
        upload = app.UploadFile(filename="scan.pdf", file=BytesIO(document.tobytes()))
        document.close()
        active = peak = invoked = 0
        lock = threading.Lock()
        first_batch = threading.Barrier(3)

        def transcribe(messages, **kwargs):
            nonlocal active, peak, invoked
            with lock:
                invoked += 1
                page = invoked
                active += 1
                peak = max(peak, active)
            if page <= 3:
                first_batch.wait(timeout=2)
            with lock:
                active -= 1
            return f"Transcription {page}"

        with patch.object(app, "answer", side_effect=transcribe) as answer:
            text = await app.read_document(upload)

        self.assertEqual(peak, 3)
        self.assertLessEqual(peak, 3)
        self.assertEqual(
            [text.index(f"[scan.pdf, page {page}]") for page in range(1, 5)],
            sorted(text.index(f"[scan.pdf, page {page}]") for page in range(1, 5)),
        )
        self.assertEqual(answer.call_count, 4)

    async def test_scanned_pdf_uses_bounded_jpeg(self):
        document = pymupdf.open()
        document.new_page(width=3000, height=4000)
        document.new_page()
        upload = app.UploadFile(filename="scan.pdf", file=BytesIO(document.tobytes()))
        document.close()
        with patch.object(app, "answer", return_value="Transcribed questions") as answer:
            text = await app.read_document(upload)
        self.assertIn("Transcribed questions", text)
        self.assertTrue(text.startswith("[scan.pdf, page 1]"))
        self.assertIn("[scan.pdf, page 2]", text)
        self.assertEqual(answer.call_count, 2)
        self.assertEqual(answer.call_args.kwargs["reasoning_effort"], "low")
        image_url = answer.call_args_list[0].args[0][0]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/jpeg;base64,"))
        image = pymupdf.Pixmap(base64.b64decode(image_url.split(",", 1)[1]))
        self.assertLessEqual(max(image.width, image.height), 2000)

    async def test_chat_uses_alternating_conversation_turns(self):
        thread_id = "test-chat"
        app.threads[thread_id] = {"context": "Paper source", "messages": []}
        try:
            with patch.object(app, "answer", return_value="A sourced reply") as answer:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
                    response = await client.post("/api/chat", json={"thread_id": thread_id, "message": "What should I revise?"})
            self.assertEqual(response.status_code, 200)
            messages = answer.call_args.args[0]
            self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant", "user"])
            self.assertIn("Paper source", messages[1]["content"])
        finally:
            app.threads.pop(thread_id, None)

    async def test_analysis_returns_job_id_and_health_responds_while_model_waits(self):
        started, release = threading.Event(), threading.Event()
        safety_timer = threading.Timer(2, release.set)
        safety_timer.start()

        responses = iter([
            {"questions": [{"paper": "test.pdf", "concept": "Algebra", "question": "Solve x=1", "marks": 1, "page": 1, "type": "Solve", "repeat": "new"}]},
            {"summary": "Test", "classifications": [{"index": 0, "repeat": "new"}], "patterns": [], "practice": []},
        ])

        def slow_answer(*args, **kwargs):
            self.assertEqual(kwargs["reasoning_effort"], "low")
            self.assertEqual(kwargs["response_format"]["type"], "json_schema")
            started.set()
            release.wait()
            return json.dumps(next(responses))

        document = pymupdf.open()
        document.new_page().insert_text((72, 72), "Solve x=1")
        pdf = document.tobytes()
        document.close()
        try:
            with patch.object(app, "answer", side_effect=slow_answer):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
                    upload = asyncio.create_task(client.post("/api/analyze", files={"papers": ("test.pdf", pdf, "application/pdf")}))
                    response = await upload
                    self.assertEqual(response.status_code, 202)
                    job_id = response.json()["job_id"]
                    while not started.is_set():
                        await asyncio.sleep(0.01)
                    response = await client.get("/api/health")
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(release.is_set(), "Health waited for the model instead of responding during analysis")
                    status = await client.get(f"/api/analyze/{job_id}")
                    self.assertEqual(status.json()["status"], "processing")
                    self.assertEqual(status.json()["progress"], "Sending test.pdf (1 pages) to Gemini for analysis")
                    release.set()
                    for _ in range(100):
                        status = await client.get(f"/api/analyze/{job_id}")
                        if status.json()["status"] != "processing":
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(status.json()["status"], "complete")
                    self.assertEqual(status.json()["result"]["paper_count"], 1)
                    self.assertEqual(status.json()["result"]["questions"][0]["question"], "Solve x=1")
                    self.assertEqual(status.json()["result"]["questions"][0]["repeat"], "new")
        finally:
            release.set()
            safety_timer.cancel()


if __name__ == "__main__":
    unittest.main()
