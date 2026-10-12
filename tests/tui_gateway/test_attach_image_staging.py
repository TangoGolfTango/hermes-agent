"""Path-based image attach must stage the image where the session's agent can read it.

Desktop attaches a picked image by host path (``image.attach``) whenever it does not yet
know the session runs in a sandbox. Under a docker profile the queued host path
(``/private/tmp/x.png``) does not exist in the container, so the agent's
``vision_analyze``/``read_file`` retries failed and it went looking through old cached
images instead (TheMaestro desktop acceptance). ``image.attach_bytes`` already stages
into the session profile's mounted ``images/`` dir; a path attach under a sandboxed
backend gets the same staging, keeping the user's original file untouched.
"""

from __future__ import annotations

import threading

import pytest

from tui_gateway import server

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def _profile_home(tmp_path, backend: str):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(f"terminal:\n  backend: {backend}\n", encoding="utf-8")
    return home


@pytest.fixture
def make_session(tmp_path, monkeypatch, request):
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    sids = []

    def make(backend: str) -> tuple[str, dict]:
        home = _profile_home(tmp_path, backend)
        sid = f"stage-{request.node.name}-{len(sids)}"
        session = {
            "agent": None, "agent_ready": threading.Event(), "agent_error": None,
            "attached_images": [], "cwd": str(tmp_path / "workspace"), "history": [],
            "history_lock": threading.RLock(), "history_version": 0, "image_counter": 0,
            "profile_home": str(home), "running": False, "session_key": sid, "transport": None,
        }
        server._sessions[sid] = session
        sids.append(sid)
        return sid, session

    yield make
    for sid in sids:
        server._sessions.pop(sid, None)


@pytest.fixture
def picked_image(tmp_path):
    source = tmp_path / "picked" / "desktop-pick.png"
    source.parent.mkdir()
    source.write_bytes(PNG_BYTES)
    return source


def call(method: str, params: dict) -> dict:
    return server._methods[method](1, params)


def test_sandboxed_path_attach_stages_into_the_profile_images_dir(make_session, picked_image):
    sid, session = make_session("docker")

    response = call("image.attach", {"session_id": sid, "path": str(picked_image)})

    assert "error" not in response, response
    staged = response["result"]["path"]
    images_dir = (server.Path(session["profile_home"]) / "images").resolve()
    assert server.Path(staged).resolve().parent == images_dir
    assert server.Path(staged).read_bytes() == PNG_BYTES
    assert session["attached_images"] == [staged]
    # The user's original file is left where it was.
    assert picked_image.read_bytes() == PNG_BYTES


def test_local_path_attach_keeps_the_host_path(make_session, picked_image):
    sid, session = make_session("local")

    response = call("image.attach", {"session_id": sid, "path": str(picked_image)})

    assert response["result"]["path"] == str(picked_image)
    assert session["attached_images"] == [str(picked_image)]
    assert not (server.Path(session["profile_home"]) / "images").exists()


def test_sandboxed_path_attach_enforces_the_upload_size_cap(make_session, picked_image, monkeypatch):
    sid, session = make_session("docker")
    monkeypatch.setattr(server, "_ATTACH_BYTES_MAX_BYTES", len(PNG_BYTES) - 1)

    response = call("image.attach", {"session_id": sid, "path": str(picked_image)})

    assert "error" in response
    assert session["attached_images"] == []
    assert not (server.Path(session["profile_home"]) / "images").exists()


def test_sandboxed_image_drop_stages_like_attach(make_session, picked_image):
    sid, session = make_session("docker")

    response = call("input.detect_drop", {"session_id": sid, "text": str(picked_image)})

    assert response["result"]["is_image"] is True
    staged = response["result"]["path"]
    assert server.Path(staged).resolve().parent == (server.Path(session["profile_home"]) / "images").resolve()
    assert session["attached_images"] == [staged]
