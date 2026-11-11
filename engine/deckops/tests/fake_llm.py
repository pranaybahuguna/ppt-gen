"""A tiny OpenAI-compatible server for tests: answers /v1/chat/completions without any real model.

It looks at the system prompt to see which call it is, and answers in the expected JSON shape,
so the real HTTP client code in controlplane/planner.py is exercised end to end.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from controlplane.planner import RENDER_SYSTEM, StubPlanner

OUTLINE = {"title": "Chatbot pilot", "footer": "Support operations", "slides": [
    {"kind": "cover", "title": "Chatbot pilot", "subtitle": "Six weeks",
     "points": [{"value": "38%", "text": "solved by the bot", "fact": "F2"},
                {"value": "2 min", "text": "first response", "fact": "F3"}]},
    {"kind": "sequence", "title": "Next steps", "points": [{"heading": "Q1", "text": "Two more teams."},
                                                           {"heading": "Q2", "text": "All teams."}]},
    {"kind": "closing", "title": "Thank you"}]}


class Handler(BaseHTTPRequestHandler):
    calls = []
    reject_temperature = False          # behave like models that only accept the default temperature

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.calls.append({"auth": self.headers.get("Authorization"), "body": body})
        if Handler.reject_temperature and "temperature" in body:
            err = json.dumps({"error": {"message": "Unsupported value: 'temperature' does not support 0"}}).encode()
            self.send_response(400)
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if system.startswith("You write the CONTENT"):
            answer = OUTLINE
        elif system == RENDER_SYSTEM:
            msg = json.loads(user)
            answer = StubPlanner().render_slide(msg["slide_content"], msg["layouts"], msg["deck"],
                                                msg.get("feedback"), msg.get("previous_answer"))
        elif "numbered" in system:                       # vision: label boxes -> no opinion
            answer = {}
        else:                                            # vision: compare test slide with example
            answer = {"same_design": True, "problems": []}
        out = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def start():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
