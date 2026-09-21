#!/usr/bin/env python3
"""Capture per-semester teaching-task reports after authenticated menu navigation."""
import json
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select


BASE = "https://qgjw.suet.edu.cn"
OUT = Path("/tmp/qgjw_tasks")


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
        driver.get(BASE + "/frame/jw/teacherstudentmenu.jsp?menucode=T201")
        time.sleep(4)
        driver.switch_to.frame("frame_1")
        semesters = [(o.get_attribute("value"), o.text.strip()) for o in Select(driver.find_element(By.ID, "sel_xnxq")).options if o.get_attribute("value")]
        results = []
        for value, label in semesters:
            Select(driver.find_element(By.ID, "sel_xnxq")).select_by_value(value)
            Select(driver.find_element(By.ID, "sel_xnxqh")).select_by_value(value)
            driver.find_element(By.ID, "btnQry").click()
            time.sleep(4)
            driver.switch_to.frame("frmReport")
            html = driver.page_source
            name = value.replace(",", "_")
            (OUT / f"task_{name}.html").write_text(html)
            results.append({"value": value, "label": label, "title": driver.title, "size": len(html)})
            driver.switch_to.parent_frame()
        (OUT / "reports.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps({"ok": True, "reports": results}, ensure_ascii=False))
    finally:
        password = ""
        driver.quit()


if __name__ == "__main__":
    main()
