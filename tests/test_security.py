"""_guard: הגנת CSRF/DNS-rebind (Origin) + PIN אופציונלי."""


def _c(app):
    return app.app.test_client()


def test_origin_mismatch_rejected(paths, sysctl, no_sleep):
    app = paths
    r = _c(app).post("/api/mode", json={"mode": "off"},
                     headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_same_origin_ok(paths, sysctl, no_sleep):
    app = paths
    r = _c(app).post("/api/mode", json={"mode": "off"},
                     headers={"Origin": "http://localhost", "Host": "localhost"})
    assert r.status_code != 403


def test_no_origin_passes(paths, sysctl, no_sleep):
    app = paths
    r = _c(app).post("/api/mode", json={"mode": "off"})
    assert r.status_code != 403


def test_pin_required_when_set(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_PIN", "1234")
    r = _c(app).post("/api/mode", json={"mode": "off"})
    assert r.status_code == 401 and r.get_json().get("auth") is True


def test_pin_accepts_correct(paths, sysctl, no_sleep, monkeypatch):
    app = paths
    monkeypatch.setattr(app, "DMR_PIN", "1234")
    r = _c(app).post("/api/mode", json={"mode": "off"}, headers={"X-DMR-PIN": "1234"})
    assert r.status_code != 401


def test_get_not_guarded(paths):
    assert _c(paths).get("/api/state").status_code == 200


def test_sudo_prefix_shape(paths):
    app = paths
    assert app.SUDO == [] or app.SUDO == ["sudo", "-n"]
