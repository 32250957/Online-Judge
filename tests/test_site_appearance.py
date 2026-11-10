import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import AuditLog, SiteAppearance, User
from app.security import hash_password
from app.site_appearance import appearance_form, appearance_snapshot, validate_appearance


NOW = datetime(2026, 11, 11, 0)


@pytest.mark.parametrize("start,end,at,status", [
    (None, None, NOW, "active"),
    (NOW, None, NOW - timedelta(seconds=1), "scheduled"),
    (NOW, None, NOW, "active"),
    (NOW, NOW + timedelta(hours=1), NOW, "active"),
    (NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=1), "ended"),
    (None, NOW, NOW, "ended"),
    (None, None, NOW + timedelta(days=3660), "active"),
])
def test_schedule_boundaries(start, end, at, status):
    row = SiteAppearance(theme="pepero", color_mode="dark", motion_enabled=False, starts_at=start, ends_at=end)
    snapshot = appearance_snapshot(row, at)
    assert snapshot["status"] == status
    assert snapshot["theme"] == ("pepero" if status == "active" else "default")
    assert snapshot["color_mode"] == "dark"
    assert snapshot["motion_enabled"] is False


def test_default_without_settings_and_aware_utc():
    assert appearance_snapshot(at=NOW)["theme"] == "default"
    row = SiteAppearance(theme="pepero", starts_at=NOW.replace(tzinfo=timezone.utc))
    assert appearance_snapshot(row, NOW)["status"] == "active"
    assert appearance_snapshot(row, NOW)["starts_at"] == "2026-11-11T00:00:00.000Z"


def form_values(**changes):
    values = dict(theme="pepero", color_mode="light", motion_enabled=True,
                  starts_at="2026-11-11T09:00", ends_at="2026-11-12T00:00", no_end=False)
    values.update(changes)
    return values


def test_kst_round_trip_and_unlimited_future_start():
    values = validate_appearance(form_values(), NOW - timedelta(hours=1))
    assert values["starts_at"] == NOW
    assert values["ends_at"] == datetime(2026, 11, 11, 15)
    assert appearance_form(SiteAppearance(**values))["starts_at"] == "2026-11-11T09:00"
    forever = validate_appearance(form_values(no_end=True, ends_at="invalid"), NOW)
    assert forever["ends_at"] is None
    assert appearance_snapshot(SiteAppearance(**forever), NOW - timedelta(seconds=1))["status"] == "scheduled"
    assert validate_appearance(form_values(starts_at="", no_end=True), NOW)["starts_at"] is None


@pytest.mark.parametrize("changes", [
    {"theme":"javascript:alert(1)"}, {"color_mode":"unknown"},
    {"starts_at":"bad"}, {"starts_at":"2026-11-11"}, {"starts_at":"2026-11-11T09:00+09:00"},
    {"ends_at":""}, {"ends_at":"2026-11-11T09:00"}, {"ends_at":"2026-11-10T09:00"},
    {"starts_at":"2020-01-01T00:00", "ends_at":"2020-01-02T00:00"},
])
def test_invalid_schedule_is_rejected(changes):
    with pytest.raises(ValueError):
        validate_appearance(form_values(**changes), NOW)


def test_default_clears_schedule():
    assert validate_appearance(form_values(theme="default", starts_at="invalid"), NOW)["starts_at"] is None
    assert validate_appearance(form_values(theme="default"), NOW)["ends_at"] is None


def login(client, db, is_admin=True):
    db.add(User(username="appearance-user", password_hash=hash_password("appearance-test-password"), is_admin=is_admin))
    db.commit()
    page = client.get("/login")
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    response = client.post("/login", data={"username":"appearance-user", "password":"appearance-test-password", "csrf_token":csrf}, follow_redirects=False)
    assert response.status_code == 303
    return csrf


@pytest.mark.parametrize("is_admin", [False, True])
def test_http_admin_permissions_csrf_and_persistence(db, app_main, is_admin):
    with TestClient(app_main.app) as client:
        assert client.get("/admin/appearance", follow_redirects=False).status_code in (303,401)
        assert client.get("/admin/appearance/preview", follow_redirects=False).status_code in (303,401)
        token = login(client, db, is_admin)
        values = dict(theme="pepero", color_mode="dark", no_end="true", motion_enabled="true")
        assert client.post("/admin/appearance", data=values).status_code == 403
        if not is_admin:
            assert client.get("/admin/appearance").status_code == 403
            assert client.get("/admin/appearance/preview").status_code == 403
            assert client.post("/admin/appearance", data={**values, "csrf_token":token}).status_code == 403
            assert db.get(SiteAppearance, 1) is None
            return
        assert client.get("/admin/appearance").status_code == 200
        saved = client.post("/admin/appearance", data={**values, "csrf_token":token}, follow_redirects=False)
        assert saved.status_code == 303
        db.expire_all()
        row = db.get(SiteAppearance, 1)
        assert row.theme == "pepero" and row.ends_at is None and row.starts_at is None
        assert row.color_mode == "dark" and row.motion_enabled
        assert db.query(AuditLog).filter(AuditLog.action == "site_appearance_update").count() == 1
        for url in ("/", "/problems", "/contests", "/login", "/admin/appearance"):
            page = client.get(url)
            assert page.status_code == 200
            assert 'class="interactive-theme event-theme event-pepero event-dark"' in page.text
        invalid = client.post("/admin/appearance", data={**values, "no_end":"false", "ends_at":"invalid", "csrf_token":token})
        assert invalid.status_code == 422 and 'value="invalid"' in invalid.text
        db.expire_all()
        assert db.get(SiteAppearance, 1).color_mode == "dark"
        assert db.query(AuditLog).filter(AuditLog.action == "site_appearance_update").count() == 1
        cleared = client.post("/admin/appearance", data={"theme":"default", "csrf_token":token}, follow_redirects=False)
        assert cleared.status_code == 303
        assert client.get("/api/site-appearance").json()["theme"] == "default"


def test_preview_never_saves_or_affects_visitors(db, app_main):
    with TestClient(app_main.app) as admin:
        login(admin, db)
        preview = admin.get("/admin/appearance/preview?theme=pepero&color_mode=dark&motion_enabled=false")
        assert preview.status_code == 200
        assert preview.headers["x-frame-options"] == "SAMEORIGIN"
        assert preview.headers["content-security-policy"] == "frame-ancestors 'self'"
        assert "event-dark event-still" in preview.text
        assert '"preview": true' in preview.text
        assert "디자인 미리보기" in preview.text
        assert "<header inert>" in preview.text and "<main inert>" in preview.text
        assert admin.get("/admin/appearance/preview?theme=unknown").status_code == 422
        assert db.get(SiteAppearance, 1) is None
        with TestClient(app_main.app) as visitor:
            home = visitor.get("/")
            assert home.headers["x-frame-options"] == "DENY"
            assert 'class="interactive-theme"' in home.text
            api = visitor.get("/api/site-appearance")
            assert api.headers["cache-control"] == "no-store"
            assert api.json()["theme"] == "default"


def test_anonymous_api_and_html_agree_with_schedule(db, app_main):
    db.add(SiteAppearance(id=1, theme="pepero", color_mode="light", ends_at=datetime(2000,1,1)))
    db.commit()
    with TestClient(app_main.app) as visitor:
        data = visitor.get("/api/site-appearance").json()
        assert data["status"] == "ended" and data["theme"] == "default"
        assert 'class="interactive-theme"' in visitor.get("/").text
        db.get(SiteAppearance, 1).ends_at = None
        db.commit()
        assert visitor.get("/api/site-appearance").json()["theme"] == "pepero"
        assert 'class="interactive-theme event-theme event-pepero event-light"' in visitor.get("/").text


def test_backup_contains_design(db, app_main):
    db.add(SiteAppearance(id=1, theme="pepero", color_mode="dark", motion_enabled=False))
    db.commit()
    with zipfile.ZipFile(io.BytesIO(app_main.build_backup_zip(db))) as archive:
        data = json.loads(archive.read("db/site_appearance.json"))
    assert data[0]["theme"] == "pepero" and data[0]["motion_enabled"] is False


def test_old_backup_without_design_is_accepted(db, app_main, monkeypatch):
    # Stop just after full archive validation, before the PostgreSQL restore.
    class ValidatedArchive(Exception):
        pass
    def stop(*args):
        raise ValidatedArchive
    monkeypatch.setattr(app_main, "safe_extract_prefix", stop)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", '{"type":"online_judge_full_backup"}')
        for model in app_main.BACKUP_MODEL_LIST:
            if model is not SiteAppearance:
                archive.writestr(f"db/{model.__tablename__}.json", "[]")
    with pytest.raises(ValidatedArchive):
        app_main.restore_backup_zip(db, buffer.getvalue())
