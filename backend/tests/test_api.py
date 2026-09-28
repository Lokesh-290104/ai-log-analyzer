from app.sample import generate_sample_logs
from tests.conftest import SMALL_LOG, final, tool_call


def upload(client, text=SMALL_LOG, filename="app.log", **data):
    return client.post("/api/uploads", files={"file": (filename, text.encode(), "text/plain")}, data=data)


def test_health(make_client):
    client, _ = make_client()
    assert client.get("/api/health").json()["database"] == "ok"


def test_upload_returns_overview_computed_without_llm(make_client):
    client, llm = make_client()
    r = upload(client)
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "app.log"
    assert body["parse_stats"]["parsed_entries"] == 6
    assert body["overview"]["error_entries"] == 3
    assert body["overview"]["top_errors"]["rows"][0]["count"] == 2
    assert body["overview"]["timeline"]["buckets"]
    assert llm.calls == []
    assert client.get(f"/api/uploads/{body['id']}").json()["overview"] == body["overview"]


def test_upload_ids_are_uuids(make_client):
    client, _ = make_client()
    assert len(upload(client).json()["id"]) == 36


def test_upload_custom_name_and_bom(make_client):
    client, _ = make_client()
    r = upload(client, text="﻿" + SMALL_LOG, name="prod.log")
    assert r.status_code == 201 and r.json()["name"] == "prod.log"
    assert r.json()["parse_stats"]["unparsed_lines"] == 0


def test_sample_upload(make_client):
    client, _ = make_client()
    body = client.post("/api/uploads/sample").json()
    assert body["parse_stats"]["parsed_entries"] > 2000
    assert body["overview"]["top_errors"]["rows"][0]["services"] == ["payments"]


def test_upload_errors(make_client):
    client, _ = make_client()
    assert upload(client, text="").status_code == 400
    r = upload(client, text="no timestamps here\nat all\n")
    assert r.status_code == 400 and "No log lines were recognized" in r.json()["detail"]
    assert client.post("/api/uploads").status_code == 422  # no file


def test_upload_too_large(make_client):
    client, _ = make_client(max_upload_bytes=1000)
    r = upload(client, text=SMALL_LOG * 10)
    assert r.status_code == 413 and "too large" in r.json()["detail"]


def test_upload_too_large_rejected_by_declared_length(make_client):
    client, _ = make_client(max_upload_bytes=1000)
    r = client.post("/api/uploads", content=b"x" * 100_000,
                    headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413


def test_upload_too_many_lines(make_client):
    client, _ = make_client(max_upload_lines=3)
    r = upload(client)
    assert r.status_code == 400 and "limit" in r.json()["detail"]


def test_unknown_upload_404(make_client):
    client, _ = make_client()
    assert client.get("/api/uploads/nope").status_code == 404
    assert client.post("/api/uploads/nope/ask", json={"question": "errors?"}).status_code == 404
    assert client.get("/api/uploads/nope/analyses").status_code == 404


def test_ask_answers_with_trace_and_persists(make_client):
    client, llm = make_client([
        tool_call("count_by", {"field": "service", "level": ["ERROR"]}),
        final("payments had 2 errors and orders had 1.", [1]),
    ])
    upload_id = upload(client).json()["id"]
    r = client.post(f"/api/uploads/{upload_id}/ask", json={"question": "  Which service errors most?  "})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered" and body["question"] == "Which service errors most?"
    assert body["verification"]["ok"] and body["verification"]["checked_numbers"] == ["2", "1"]
    assert body["trace"][0]["result"]["rows"][0] == {"value": "payments", "count": 2, "share_pct": 66.7}
    assert body["model"] == "fake-llm" and body["llm_calls"] == 2
    history = client.get(f"/api/uploads/{upload_id}/analyses").json()
    assert [a["id"] for a in history] == [body["id"]]


def test_ask_fail_closed_is_stored_and_has_no_answer(make_client):
    client, _ = make_client(["not json", "still not json", "nope"])
    upload_id = upload(client).json()["id"]
    body = client.post(f"/api/uploads/{upload_id}/ask", json={"question": "errors?"}).json()
    assert body["status"] == "failed" and body["answer"] is None
    assert body["error_code"] == "invalid_output" and len(body["trace"]) == 3
    assert client.get(f"/api/uploads/{upload_id}/analyses").json()[0]["status"] == "failed"


def test_ask_llm_unavailable_is_503(make_client):
    from app.llm import LLMError

    client, _ = make_client([LLMError("The AI service couldn't answer.")])
    upload_id = upload(client).json()["id"]
    r = client.post(f"/api/uploads/{upload_id}/ask", json={"question": "errors?"})
    assert r.status_code == 503 and "couldn't answer" in r.json()["detail"]


def test_ask_without_llm_configured_is_503(settings, make_client):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(replace(settings, llm_provider="gemini", gemini_api_key=""))
    with TestClient(app) as client:
        upload_id = upload(client).json()["id"]
        r = client.post(f"/api/uploads/{upload_id}/ask", json={"question": "errors?"})
    assert r.status_code == 503 and "GEMINI_API_KEY" in r.json()["detail"]


def test_ask_validation(make_client):
    client, _ = make_client()
    upload_id = upload(client).json()["id"]
    for bad in ({"question": "  a "}, {"question": "x" * 501}, {}):
        assert client.post(f"/api/uploads/{upload_id}/ask", json=bad).status_code == 422


def test_ask_rate_limited_before_llm(make_client):
    client, llm = make_client([final("No numbers.", [])], ask_limit_per_minute=1)
    upload_id = upload(client).json()["id"]
    assert client.post(f"/api/uploads/{upload_id}/ask", json={"question": "one?"}).status_code == 200
    r = client.post(f"/api/uploads/{upload_id}/ask", json={"question": "two?"})
    assert r.status_code == 429 and "per minute" in r.json()["detail"]
    assert len(llm.calls) == 1


def test_upload_rate_limited(make_client):
    client, _ = make_client(upload_limit_per_minute=1)
    assert upload(client).status_code == 201
    assert upload(client).status_code == 429


def test_trust_proxy_uses_last_forwarded_address(make_client):
    client, _ = make_client([final("ok", [])] * 2, ask_limit_per_minute=1, trust_proxy=True)
    upload_id = upload(client).json()["id"]
    ask = lambda ip: client.post(f"/api/uploads/{upload_id}/ask", json={"question": "hi?"},
                                 headers={"x-forwarded-for": f"6.6.6.6, {ip}"})
    assert ask("1.1.1.1").status_code == 200
    assert ask("2.2.2.2").status_code == 200  # different client, own budget
    assert ask("1.1.1.1").status_code == 429


def test_entries_reload_from_database_when_not_cached(make_client):
    client, _ = make_client([tool_call("summary"), final("6 entries.", [1])], entry_cache_entries=0)
    upload_id = upload(client).json()["id"]
    body = client.post(f"/api/uploads/{upload_id}/ask", json={"question": "how many?"}).json()
    assert body["status"] == "answered"
    assert body["trace"][0]["result"]["first_ts"] == "2026-09-28T10:00:00Z"  # UTC survives the round trip


def test_serves_frontend_when_built(settings, tmp_path):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app.main import create_app

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>app</h1>")
    with TestClient(create_app(replace(settings, static_dir=static))) as client:
        assert "app" in client.get("/").text
        assert client.get("/api/health").status_code == 200


def test_sample_log_end_to_end_numbers(make_client):
    client, _ = make_client([
        tool_call("top_messages", {"limit": 1}),
        final("The top error is a payments database timeout.", [1]),
    ])
    upload_id = client.post("/api/uploads/sample").json()["id"]
    body = client.post(f"/api/uploads/{upload_id}/ask", json={"question": "top error?"}).json()
    assert body["status"] == "answered"
    assert body["trace"][0]["result"]["rows"][0]["services"] == ["payments"]
    assert len(generate_sample_logs()) < 1_000_000


def test_entry_cache_is_bounded_by_total_entries():
    from app.db import EntryCache
    from app.parser import parse_logs

    entries = parse_logs(SMALL_LOG).entries  # 6 entries
    cache = EntryCache(max_entries=10)
    cache.put("a", entries)
    cache.put("b", entries)  # 12 > 10: evicts "a"
    assert cache.get("a") is None and cache.get("b") is entries
    cache.put("b", entries)  # re-put doesn't double count
    cache.put("c", entries[:4])
    assert cache.get("b") is entries and cache.get("c") is not None
    cache.put("huge", entries * 2)  # larger than the whole budget: not cached
    assert cache.get("huge") is None and cache.get("b") is entries


def test_follow_up_sends_recent_answered_questions(make_client):
    client, llm = make_client([
        final("First answer.", []), "garbage", "garbage", "garbage", final("Second answer.", []),
        final("Third answer.", []),
    ], history_turns=2)
    upload_id = upload(client).json()["id"]
    for q in ("first question?", "failing question?", "second question?", "third question?"):
        client.post(f"/api/uploads/{upload_id}/ask", json={"question": q})
    last_prompt = llm.calls[-1][1][0].content
    # Failed analyses are not context; only the 2 most recent answered ones, oldest first.
    assert "failing question?" not in last_prompt and "first question?" in last_prompt
    assert last_prompt.index("first question?") < last_prompt.index("second question?")
    assert last_prompt.endswith("Question: third question?")


def test_history_can_be_disabled(make_client):
    client, llm = make_client([final("One.", []), final("Two.", [])], history_turns=0)
    upload_id = upload(client).json()["id"]
    client.post(f"/api/uploads/{upload_id}/ask", json={"question": "first?"})
    client.post(f"/api/uploads/{upload_id}/ask", json={"question": "second?"})
    assert llm.calls[-1][1][0].content == "Question: second?"
