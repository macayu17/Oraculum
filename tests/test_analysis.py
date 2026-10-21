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
    def test_reasoning_mode_is_sent_to_nvidia(self):
        create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="A reply"), finish_reason="stop")]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(app, "get_client", return_value=client):
            self.assertEqual(app.answer([], reasoning_effort="none"), "A reply")
        self.assertEqual(create.call_args.kwargs["reasoning_effort"], "none")
        self.assertNotIn("extra_body", create.call_args.kwargs)


    def test_truncated_completion_is_not_returned_as_a_report(self):
        create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="partial"), finish_reason="length")]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(app, "get_client", return_value=client):
            with self.assertRaises(app.HTTPException) as error:
                app.answer([])
        self.assertEqual(error.exception.status_code, 502)
        self.assertIn("response limit", error.exception.detail)


class UploadResponsivenessTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(answer.call_args.kwargs["reasoning_effort"], "none")
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

    async def test_health_responds_while_analysis_waits_for_model(self):
        started, release = threading.Event(), threading.Event()
        safety_timer = threading.Timer(2, release.set)
        safety_timer.start()

        def slow_answer(*args, **kwargs):
            self.assertEqual(kwargs["reasoning_effort"], "none")
            self.assertEqual(kwargs["response_format"]["type"], "json_schema")
            started.set()
            release.wait()
            return json.dumps({"summary": "Test", "questions": [{"paper": "test.pdf", "concept": "Algebra", "question": "Solve x=1", "marks": 1, "page": 1, "type": "Solve", "repeat": "new"}], "patterns": [], "practice": []})

        document = pymupdf.open()
        document.new_page().insert_text((72, 72), "Solve x=1")
        pdf = document.tobytes()
        document.close()
        try:
            with patch.object(app, "answer", side_effect=slow_answer):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
                    upload = asyncio.create_task(client.post("/api/analyze", files={"papers": ("test.pdf", pdf, "application/pdf")}))
                    while not started.is_set():
                        await asyncio.sleep(0.01)
                    response = await client.get("/api/health")
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(release.is_set(), "Health waited for the model instead of responding during analysis")
                    release.set()
                    self.assertEqual((await upload).status_code, 200)
        finally:
            release.set()
            safety_timer.cancel()


if __name__ == "__main__":
    unittest.main()
