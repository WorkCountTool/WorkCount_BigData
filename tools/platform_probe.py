#!/usr/bin/env python3
"""Temporary authenticated probe. Credentials are read from stdin and never persisted."""
import json
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


def main():
    username = sys.stdin.readline().strip()
    password = sys.stdin.readline().strip()
    if not username or not password:
        raise SystemExit("credentials missing")
    options = Options()
    options.binary_location = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    options.add_argument("--headless=new")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
    driver = webdriver.Chrome(options=options)
    try:
        driver.get("https://qgjw.suet.edu.cn/cas/login.action")
        driver.find_element(By.ID, "username1").click()
        driver.find_element(By.ID, "username").send_keys(username)
        driver.find_element(By.ID, "password1").click()
        driver.find_element(By.ID, "password").send_keys(password)
        driver.find_element(By.ID, "login").click()
        deadline = time.time() + 20
        while time.time() < deadline:
            if "/cas/login.action" not in driver.current_url and "cas/logon.action" not in driver.current_url:
                break
            message = driver.find_element(By.ID, "msg").text.strip()
            if message and "正在登录" not in message:
                break
            time.sleep(.5)
        result = {
            "url": driver.current_url,
            "title": driver.title,
            "message": driver.find_element(By.ID, "msg").text.strip() if driver.find_elements(By.ID, "msg") else "",
            "cookies": [c["name"] for c in driver.get_cookies()],
        }
        Path("/tmp/qgjw_probe_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        Path("/tmp/qgjw_after_login.html").write_text(driver.page_source)
        driver.save_screenshot("/tmp/qgjw_after_login.png")
        logs = driver.get_log("performance")
        urls = []
        for entry in logs:
            try:
                message = json.loads(entry["message"])["message"]
                if message["method"] == "Network.requestWillBeSent":
                    request = message["params"]["request"]
                    urls.append({"method": request["method"], "url": request["url"]})
            except Exception:
                pass
        Path("/tmp/qgjw_network.json").write_text(json.dumps(urls, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        password = ""
        driver.quit()


if __name__ == "__main__":
    main()
