"""Optional end-to-end checks: pip install playwright; playwright install."""
import argparse
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server


def run(engines, screenshots=None):
    with sync_playwright() as playwright:
        for engine in engines:
            width = 390 if engine == "webkit" else 1280
            with tempfile.TemporaryDirectory() as temp:
                app = create_app({"DATABASE": str(Path(temp) / "test.sqlite3")})
                server = make_server("127.0.0.1", 0, app, threaded=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                browser = getattr(playwright, engine).launch()
                context = browser.new_context(viewport={"width": width, "height": 850})
                context.route("https://example.com/**", lambda route: route.fulfill(body="A saved article", content_type="text/plain"))
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    page.goto(f"http://127.0.0.1:{server.server_port}")
                    expect(page.locator("#counts")).to_have_text("0 unread · 0 saved")
                    page.get_by_role("button", name="Add a link", exact=True).click()
                    page.get_by_label("Link", exact=True).fill("https://example.com/article")
                    page.get_by_label("Description (optional)").fill("<b>A thoughtful article</b>")
                    page.get_by_role("button", name="Save for later +").click()
                    expect(page.get_by_role("status")).to_have_text("Saved for later.")
                    page.get_by_role("button", name="Pick a link", exact=True).click()
                    with context.expect_page() as opened:
                        page.locator("#pop").click()
                    popup = opened.value
                    expect(popup).to_have_url("https://example.com/article")
                    expect(page.locator("#counts")).to_have_text("0 unread · 1 saved")
                    expect(page.locator("#last-description")).to_have_text("<b>A thoughtful article</b>")
                    assert page.locator("#last-description b").count() == 0
                    page.locator("#star").click()
                    expect(page.locator("#star")).to_have_text("★ Unstar")
                    page.reload()
                    expect(page.locator("#star")).to_have_text("★ Unstar")
                    with context.expect_page() as reopened:
                        page.get_by_role("link", name="Open again").click()
                    expect(reopened.value).to_have_url("https://example.com/article")
                    page.get_by_role("button", name="Starred", exact=False).first.click()
                    expect(page.locator("#starred-list article")).to_have_count(1)
                    with context.expect_page() as favorite:
                        page.locator("#pop-starred").click()
                    expect(favorite.value).to_have_url("https://example.com/article")
                    expect(page.locator("#read")).to_be_visible()
                    page.locator("#star").click()
                    expect(page.locator("#star")).to_have_text("☆ Star")
                    page.get_by_role("button", name="Starred", exact=False).first.click()
                    expect(page.locator("#starred-list")).to_contain_text("No favorites yet")
                    page.get_by_role("button", name="Pick a link", exact=True).click()
                    page.locator("#pop").click()
                    expect(page.get_by_role("status")).to_have_text("No links in this selection yet.")
                    page.select_option("#mode", "all")
                    # Exercise popup-blocked recovery without relying on browser settings.
                    page.evaluate("window.open = () => null")
                    page.locator("#pop").click()
                    expect(page.get_by_role("status")).to_contain_text("browser blocked")
                    expect(page.locator("#reopen")).to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    if screenshots:
                        screenshots.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(screenshots / f"{engine}-desktop.png"), full_page=True)
                    # Also check a narrow layout in every engine.
                    page.set_viewport_size({"width": 360, "height": 780})
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    if screenshots:
                        page.screenshot(path=str(screenshots / f"{engine}-mobile.png"), full_page=True)
                    page.locator("#backups summary").click()
                    with page.expect_download() as download:
                        page.get_by_role("link", name="Download backup").click()
                    saved_file = Path(temp) / "downloaded.json"
                    download.value.save_as(saved_file)
                    backup = json.loads(saved_file.read_text())
                    assert backup["app"] == "Later" and len(backup["items"]) == 1
                    page.get_by_role("button", name="Add a link", exact=True).click()
                    page.get_by_label("Link", exact=True).fill("https://example.com/extra")
                    page.get_by_role("button", name="Save for later +").click()
                    expect(page.get_by_role("status")).to_have_text("Saved for later.")
                    # Cancel must leave the queue untouched.
                    page.locator("#backup-file").set_input_files(saved_file)
                    page.once("dialog", lambda dialog: dialog.dismiss())
                    page.get_by_role("button", name="Restore backup", exact=False).click()
                    expect(page.get_by_role("button", name="Restore backup", exact=False)).to_be_enabled()
                    expect(page.locator("#counts")).to_have_text("1 unread · 2 saved")
                    page.once("dialog", lambda dialog: dialog.accept())
                    page.get_by_role("button", name="Restore backup", exact=False).click()
                    expect(page.get_by_role("status")).to_have_text("Backup restored. 1 saved links loaded.")
                    expect(page.locator("#counts")).to_have_text("0 unread · 1 saved")
                    page.reload()
                    expect(page.locator("#counts")).to_have_text("0 unread · 1 saved")
                    page.locator("#backups summary").click()
                    page.locator("#backup-file").set_input_files({"name": "bad.json", "mimeType": "application/json", "buffer": b"not json"})
                    page.get_by_role("button", name="Restore backup", exact=False).click()
                    expect(page.get_by_role("status")).to_have_text("Choose a valid Later JSON backup file.")
                    expect(page.locator("#counts")).to_have_text("0 unread · 1 saved")
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    if screenshots:
                        page.screenshot(path=str(screenshots / f"{engine}-backups-mobile.png"), full_page=True)
                    page.get_by_role("button", name="Pick a link", exact=True).click()
                    page.get_by_role("button", name="Save for later again", exact=True).click()
                    expect(page.get_by_role("status")).to_contain_text("This link is unread")
                    expect(page.locator("#counts")).to_have_text("1 unread · 1 saved")
                    expect(page.locator("#requeue")).to_be_disabled()
                    page.reload()
                    expect(page.locator("#counts")).to_have_text("1 unread · 1 saved")
                    expect(page.locator("#requeue")).to_be_disabled()
                    page.evaluate("window.open = () => null")
                    page.locator("#pop").click()
                    expect(page.locator("#counts")).to_have_text("0 unread · 1 saved")
                    expect(page.locator("#requeue")).to_be_enabled()
                    assert not errors, errors
                    print(f"PASS {engine}: add, pop/new tab, read state, star/unstar, reload, reopen, starred pop, empty queue, blocked popup, responsive layout, backup download/upload, cancel restore, invalid file, mark unread and repick")
                finally:
                    browser.close()
                    server.shutdown()
                    thread.join()
                    server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browsers", nargs="+", choices=("chromium", "firefox", "webkit"), default=["chromium", "firefox", "webkit"])
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    run(args.browsers, args.screenshots)
