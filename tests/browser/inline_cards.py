"""Run with python tests/browser/inline_cards.py after npm run build.

Uses Playwright Chromium in an isolated context. All HTTP requests are served
by the fixture, including the built frontend. No running host or model needed.
"""
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
READY = {"ready": True, "questions": [], "conflicts": [], "interpretations_required": []}
OPTIONS = [{"label": "L1", "value": 1}, {"label": "L2", "value": 2}]
FORM = {"ask_id": "ask", "questions": [
    {"id": "base", "text": "Base level?", "options": OPTIONS},
    {"id": "levels", "text": "Other levels?", "options": OPTIONS, "multiple": True, "allow_other": True},
]}


def event(name, payload):
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n"


def card(name):
    spec = {"task": name, "action": {"kind": "run_tool", "tool": "create_wall"}, "parameters": [
        {"name": "type_name", "value": "Basic Wall", "source": "designer", "evidence": "Basic Wall"}],
        "interpretations": [], "snapshot_fingerprint": "sample"}
    return event("spec", {"spec": spec, "card": name, "errors": [], "reconcile": READY})


def check(page):
    confirms, runs, messages, errors = [], [], [], []
    reject_first_answer = True
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route_request(route):
        nonlocal reject_first_answer
        path = urlparse(route.request.url).path
        if path == "/config.json":
            route.fulfill(json={"features": {"mode": "local", "serverModel": True}})
        elif path == "/api/chat":
            body = route.request.post_data_json
            if body.get("answers") and reject_first_answer:
                reject_first_answer = False
                route.fulfill(status=400, json={"detail": "Select the device and try again"})
                return
            messages.append(body)
            message = body.get("message")
            if message in ("Ask", "Ask again"):
                reply = event("questions", FORM)
            elif message == "Evening":
                reply = 'data: "Understood."\n\n'
            elif message == "Revise":
                reply = card("Revised wall")
            elif body.get("execution"):
                reply = 'data: "Done."\n\n'
            else:
                reply = card("Original wall")
            route.fulfill(content_type="text/event-stream", headers={"X-Session-Id": "browser-test"},
                          body=reply + "event: done\ndata: [DONE]\n\n")
        elif path.endswith("/spec/confirm"):
            confirms.append(route.request.post_data_json)
            route.fulfill(json={"token": f"once-{len(confirms)}", "spec_hash": "hash",
                                "expires_at": "2026-11-25", "card": "ready"})
        elif path.endswith("/create_wall/run"):
            runs.append(route.request.post_data_json)
            if len(runs) == 1:
                route.fulfill(json={"success": False, "error": "confirmation_invalid", "reason": "expired"})
            else:
                route.fulfill(json={"success": True, "tool": "create_wall", "evidence_id": "example"})
        elif path.endswith("/snapshot"):
            route.fulfill(status=503, json={"error": "revit_unreachable"})
        elif path.startswith("/api/"):
            route.fulfill(json={})
        else:
            file = DIST / (path.lstrip("/") or "index.html")
            if file.is_file() and DIST in file.resolve().parents:
                route.fulfill(path=file)
            else:
                route.abort()

    page.route("**/*", route_request)
    page.goto("https://inline.test/")
    page.get_by_role("button", name="Task", exact=True).click()

    def say(text):
        box = page.get_by_role("combobox", name="Message")
        box.fill(text)
        box.press("Enter")

    say("Ask")
    form = page.locator(".chat-bubble.assistant form").last
    submit = form.get_by_role("button", name="继续")
    expect(submit).to_be_disabled()
    form.get_by_role("radio", name="L1").focus()
    page.keyboard.press("Space")
    expect(submit).to_be_disabled()
    form.get_by_role("checkbox", name="L2").check()
    form.get_by_role("checkbox", name="其他").check()
    expect(submit).to_be_disabled()
    form.get_by_role("textbox").fill("Mezzanine")
    submit.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_text("Select the device and try again", exact=False)).to_be_visible()
    expect(submit).to_be_enabled()
    submit.click()
    active = page.locator('.chat-bubble [data-active-card="true"]')
    expect(active).to_contain_text("Original wall")
    assert messages[-1]["answers"] == {"ask_id": "ask", "items": [
        {"id": "base", **OPTIONS[0]}, {"id": "levels", **OPTIONS[1]},
        {"id": "levels", "label": "Mezzanine", "value": "Mezzanine"}]}
    assert "1. Base level?" in messages[-1]["message"]
    say("Revise")
    expect(page.get_by_text("已被新卡替换", exact=True)).to_have_count(1)
    expect(active).to_contain_text("Revised wall")
    say("同意")
    expect(page.get_by_text("卡片在我上一条提交它的回复里。", exact=False)).to_be_visible()
    assert not runs and not confirms
    active.get_by_role("button", name="确认并执行").click()
    expect(active).to_contain_text("expired")
    active.get_by_role("button", name="重试执行").click()
    expect(active).to_contain_text("已执行")
    assert [r["token"] for r in runs] == ["once-1", "once-2"]
    assert len(confirms) == 2
    say("Ask again")
    expect(form.get_by_role("button", name="继续")).to_be_visible()
    say("Evening")
    expect(form).to_contain_text("已用文字回答")
    assert "answers" not in messages[-1]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    for node in page.locator(".chat-scroll, .spec-card, .inline-form").all():
        assert node.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), node.get_attribute("class")
    assert not errors, errors


if __name__ == "__main__":
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width in (360, 1100):
            context = browser.new_context(viewport={"width": width, "height": 800})
            check(context.new_page())
            context.close()
            print(f"Inline forms, card replacement, retry and keyboard checks passed at {width}px.")
        browser.close()
