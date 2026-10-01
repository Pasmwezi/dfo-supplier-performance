"""Optional real Chromium regression: pip install playwright; playwright install chromium."""
import socket
import threading
import time
import secrets

import pytest
from fastapi.testclient import TestClient
import uvicorn

from app.main import create_app

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def live_form(tmp_path, monkeypatch):
    monkeypatch.setenv("SPM_ATTACHMENT_DIR", str(tmp_path / "attachments"))
    token = secrets.token_urlsafe(32)
    app = create_app(f"sqlite:///{tmp_path / 'browser.db'}", seed=False, auth_tokens={
        token: {"name": "Browser Evaluator", "roles": ["ADMIN", "EVALUATOR"]},
    })
    api = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    supplier = api.post("/api/suppliers", json={"name": "Browser Shared Supplier"}).json()
    contract = api.post("/api/contracts", json={"supplier_id": supplier["id"],
        "contract_number": "BROWSER-ORIGINAL", "procurement_type": "Construction", "region": "Atlantic"}).json()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        assert api.get("/health").json() == {"status": "ok"}
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context()
            # Authorize only this temporary app; never leak auth to external fonts.
            context.route(origin + "/**", lambda route: route.continue_(headers={
                **route.request.headers, "Authorization": f"Bearer {token}",
            }))
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            # Avoid unavailable third-party font network noise in isolated QA.
            context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(body=""))
            context.route("https://fonts.gstatic.com/**", lambda route: route.fulfill(body=""))
            yield page, api, origin, supplier, contract, errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        app.state.engine.dispose()


def fill_evaluation(page):
    for name, value in {
        "description_of_work": "Isolated browser regression evaluation.",
        "project_manager_name": "Test Manager", "project_manager_email": "qa@example.org",
        "contract_award_amount": "1000", "contract_award_date": "2026-01-01",
    }.items():
        page.locator(f'[name="{name}"]').fill(value)
    for input_field in page.locator('#criteria-construction [data-score]').all():
        input_field.fill("15")


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 1000}, {"width": 390, "height": 844}])
def test_browser_existing_supplier_new_contract_atomic_flow(live_form, tmp_path, viewport):
    page, api, origin, supplier, original, errors = live_form
    page.set_viewport_size(viewport)
    page.goto(origin + "/evaluations/new")
    assert page.locator('[name="contract_id"]').is_enabled()
    assert page.locator('[name="supplier_id"]').is_disabled()
    # Switching modes must remove every inactive identification field from FormData.
    page.locator('[name="record_mode"][value="new"]').check()
    page.locator('[name="new_supplier_name"]').fill("Do Not Create This Supplier")
    page.locator('[name="record_mode"][value="new_contract"]').check()
    assert page.locator('[name="supplier_id"]').is_enabled()
    assert page.locator('[name="supplier_id"]').get_attribute("required") is not None
    assert page.locator('[name="new_supplier_name"]').is_disabled()
    assert page.locator('[name="contract_id"]').is_disabled()
    assert page.locator('[name="new_contract_number"]').is_enabled()
    dimensions = page.evaluate("({width:innerWidth, doc:document.documentElement.scrollWidth, body:document.body.scrollWidth})")
    assert dimensions["width"] == viewport["width"]
    assert dimensions["doc"] <= viewport["width"] and dimensions["body"] <= viewport["width"]
    assert not page.locator('[name="supplier_id"]').evaluate('(input) => input.checkValidity()')
    page.locator('[name="supplier_id"]').select_option(str(supplier["id"]))
    page.locator('[name="new_contract_number"]').fill("BROWSER-SECOND")
    fill_evaluation(page)
    page.screenshot(path=str(tmp_path / "new-contract-form.png"), full_page=True)
    with page.expect_response(lambda response: response.url == origin + "/api/evaluations" and response.request.method == "POST") as response:
        page.get_by_role("button", name="Save draft").click()
    assert response.value.status == 201, response.value.text()
    sent = response.value.request.post_data_json
    assert sent["supplier_id"] == supplier["id"]
    assert "new_supplier" not in sent and "contract_id" not in sent
    assert sent["new_contract"]["contract_number"] == "BROWSER-SECOND"
    page.wait_for_url("**/evaluations/*/view")
    assert page.get_by_text("Browser Shared Supplier", exact=True).first.is_visible()
    assert len(api.get("/api/suppliers").json()) == 1
    assert len(api.get("/api/contracts").json()) == 2
    assert len(api.get("/api/evaluations").json()) == 1
    # Retry a duplicate through the real form; no extra evaluation or contract.
    page.goto(origin + "/evaluations/new")
    page.locator('[name="record_mode"][value="new_contract"]').check()
    page.locator('[name="supplier_id"]').select_option(str(supplier["id"]))
    page.locator('[name="new_contract_number"]').fill(original["contract_number"])
    fill_evaluation(page)
    with page.expect_response(lambda response: response.url == origin + "/api/evaluations" and response.request.method == "POST") as response:
        page.get_by_role("button", name="Save draft").click()
    assert response.value.status == 409
    assert page.locator('#form-message').inner_text()
    assert len(api.get("/api/evaluations").json()) == 1
    assert len(api.get("/api/contracts").json()) == 2
    # Chromium logs HTTP 409 as a resource error; reject all other errors.
    assert not [error for error in errors if "409" not in error], errors


@pytest.mark.parametrize("mode", ["existing", "new"])
def test_browser_legacy_modes_still_submit_after_mode_switch(live_form, mode):
    page, api, origin, supplier, original, errors = live_form
    page.goto(origin + "/evaluations/new")
    page.locator('[name="record_mode"][value="new_contract"]').check()
    page.locator('[name="supplier_id"]').select_option(str(supplier["id"]))
    page.locator(f'[name="record_mode"][value="{mode}"]').check()
    assert page.locator('[name="supplier_id"]').is_disabled()
    if mode == "new":
        assert page.locator('[name="contract_id"]').is_disabled()
        page.locator('[name="new_supplier_name"]').fill("Browser New Supplier")
        page.locator('[name="new_contract_number"]').fill("BROWSER-NEW")
    else:
        assert page.locator('[name="new_contract_number"]').is_disabled()
        assert page.locator('[name="new_supplier_name"]').is_disabled()
    fill_evaluation(page)
    with page.expect_response(lambda response: response.url == origin + "/api/evaluations" and response.request.method == "POST") as response:
        page.get_by_role("button", name="Save draft").click()
    assert response.value.status == 201, response.value.text()
    sent = response.value.request.post_data_json
    assert "supplier_id" not in sent
    if mode == "new":
        assert sent["new_supplier"]["name"] == "Browser New Supplier"
        assert len(api.get("/api/suppliers").json()) == 2
        assert len(api.get("/api/contracts").json()) == 2
    else:
        assert sent["contract_id"] == original["id"]
        assert "new_supplier" not in sent and "new_contract" not in sent
        assert len(api.get("/api/suppliers").json()) == 1
        assert len(api.get("/api/contracts").json()) == 1
    page.wait_for_url("**/evaluations/*/view")
    assert not errors, errors
