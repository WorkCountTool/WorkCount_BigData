#!/usr/bin/env python3
"""Extract authenticated source pages and semester reports for connector development."""
import json
import re
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select


OUT = Path("/tmp/qgjw_extract")
BASE = "https://qgjw.suet.edu.cn"


def safe_name(value):
    return re.sub(r"[^0-9A-Za-z_-]+", "_", value).strip("_")


def snapshot(driver, name):
    (OUT / f"{name}.html").write_text(driver.page_source)
    fields = []
    for el in driver.find_elements(By.CSS_SELECTOR, "input, select, button, textarea"):
        try:
            options = []
            if el.tag_name == "select":
                options = [{"value": o.get_attribute("value"), "text": o.text.strip()} for o in el.find_elements(By.TAG_NAME, "option")]
            fields.append({
                "tag": el.tag_name, "id": el.get_attribute("id"), "name": el.get_attribute("name"),
                "type": el.get_attribute("type"), "value": el.get_attribute("value"),
                "text": el.text.strip(), "options": options,
            })
        except Exception:
            pass
    (OUT / f"{name}_fields.json").write_text(json.dumps(fields, ensure_ascii=False, indent=2))


def main():
    username = sys.stdin.readline().strip()
    password = sys.stdin.readline().strip()
    if not username or not password:
        raise SystemExit("credentials missing")
    OUT.mkdir(parents=True, exist_ok=True)
    options = Options()
    options.binary_location = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    for arg in ("--headless=new", "--ignore-certificate-errors", "--window-size=1440,1000", "--disable-gpu", "--no-sandbox"):
        options.add_argument(arg)
    options.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
    driver = webdriver.Chrome(options=options)
    try:
        driver.get(BASE + "/cas/login.action")
        driver.find_element(By.ID, "username1").click()
        driver.find_element(By.ID, "username").send_keys(username)
        driver.find_element(By.ID, "password1").click()
        driver.find_element(By.ID, "password").send_keys(password)
        driver.find_element(By.ID, "login").click()
        deadline = time.time() + 25
        while time.time() < deadline and "/cas/" in driver.current_url:
            time.sleep(.5)
        if "/cas/" in driver.current_url:
            raise SystemExit("login failed")
        password = ""
        time.sleep(3)

        pages = {
            "course_tasks": "/frame/jw/teacherstudentmenu.jsp?menucode=T201",
            "progress_entry": "/frame/jw/teacherstudentmenu.jsp?menucode=T20202",
            "thesis_entry": "/frame/jw/teacherstudentmenu.jsp?menucode=T7054",
        }
        for name, path in pages.items():
            driver.get(BASE + path)
            time.sleep(4)
            driver.switch_to.frame("frame_1")
            snapshot(driver, name)
            driver.switch_to.default_content()

        driver.get(BASE + "/frame/homes.action")
        time.sleep(2)
        driver.execute_async_script(
            "const done=arguments[arguments.length-1]; fetch('/wjstgdfw/jxap.jxapb.html?menucode=T20201', {credentials:'same-origin'}).then(r=>done(r.status)).catch(e=>done(String(e)));"
        )
        driver.get(BASE + "/wjstgdfw/jxap.jxapb.html?menucode=T20201")
        time.sleep(3)
        semester_select = Select(driver.find_element(By.ID, "xnxq"))
        semesters = [(o.get_attribute("value"), o.text.strip()) for o in semester_select.options if o.get_attribute("value")]
        reports = []
        for value, label in semesters:
            Select(driver.find_element(By.ID, "xnxq")).select_by_value(value)
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", driver.find_element(By.ID, "xnxq"))
            driver.find_element(By.ID, "btnQry").click()
            time.sleep(4)
            try:
                driver.switch_to.frame("frmReport")
                name = "schedule_" + safe_name(value)
                (OUT / f"{name}.html").write_text(driver.page_source)
                reports.append({"value": value, "label": label, "url": driver.current_url, "title": driver.title, "size": len(driver.page_source)})
                driver.switch_to.default_content()
            except Exception as exc:
                reports.append({"value": value, "label": label, "error": str(exc)})
                driver.switch_to.default_content()
        (OUT / "schedule_reports.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2))

        requests = []
        for entry in driver.get_log("performance"):
            try:
                msg = json.loads(entry["message"])["message"]
                if msg["method"] == "Network.requestWillBeSent":
                    req = msg["params"]["request"]
                    if "qgjw.suet.edu.cn" in req["url"]:
                        requests.append({"method": req["method"], "url": req["url"], "postData": req.get("postData", "")})
            except Exception:
                pass
        (OUT / "network.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))
        print(json.dumps({"ok": True, "semesters": semesters, "reports": reports}, ensure_ascii=False))
    finally:
        password = ""
        driver.quit()


if __name__ == "__main__":
    main()
