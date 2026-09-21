#!/usr/bin/env python3
"""Inspect authenticated teaching-platform pages without persisting credentials."""
import json
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


OUT = Path("/tmp/qgjw_explore")


def fetch(driver, url, method="GET", body=""):
    return driver.execute_async_script(
        """
        const url = arguments[0], method = arguments[1], body = arguments[2];
        const done = arguments[arguments.length - 1];
        const options = {method, credentials: 'same-origin'};
        if (method !== 'GET') {
          options.headers = {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'};
          options.body = body;
        }
        fetch(url, options).then(async response => done({
          status: response.status,
          url: response.url,
          text: await response.text()
        })).catch(error => done({error: String(error)}));
        """,
        url,
        method,
        body,
    )


def save_json(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    username = sys.stdin.readline().strip()
    password = sys.stdin.readline().strip()
    if not username or not password:
        raise SystemExit("credentials missing")

    OUT.mkdir(parents=True, exist_ok=True)
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
        deadline = time.time() + 25
        while time.time() < deadline:
            if "/cas/login.action" not in driver.current_url and "cas/logon.action" not in driver.current_url:
                break
            time.sleep(.5)
        if "/cas/" in driver.current_url:
            raise SystemExit("login failed")
        password = ""
        time.sleep(4)

        responses = {}
        probes = {
            "year_term_common": ("/jw/common/showYearTerm.action", "POST", ""),
            "year_term_user": ("/frame/desk/showYearTerm4User.action", "POST", ""),
            "teaching_tasks": ("/frame/jw/teacherstudentmenu.jsp?menucode=T201", "GET", ""),
            "teaching_progress": ("/frame/jw/teacherstudentmenu.jsp?menucode=T20202", "GET", ""),
            "practice_teaching": ("/frame/jw/teacherstudentmenu.jsp?menucode=T7054", "GET", ""),
            "arrangement": ("/wjstgdfw/jxap.jxapb.html?menucode=T20201", "GET", ""),
        }
        for name, args in probes.items():
            response = fetch(driver, *args)
            responses[name] = {k: v for k, v in response.items() if k != "text"}
            (OUT / f"{name}.html").write_text(response.get("text", ""))
        save_json("responses.json", responses)

        driver.get("https://qgjw.suet.edu.cn/wjstgdfw/jxap.jxapb.html?menucode=T20201")
        time.sleep(4)
        (OUT / "arrangement_rendered.html").write_text(driver.page_source)
        driver.save_screenshot(str(OUT / "arrangement.png"))

        elements = []
        for element in driver.find_elements(By.CSS_SELECTOR, "input, select, button, a"):
            try:
                elements.append({
                    "tag": element.tag_name,
                    "id": element.get_attribute("id"),
                    "name": element.get_attribute("name"),
                    "type": element.get_attribute("type"),
                    "text": element.text.strip(),
                    "value": element.get_attribute("value"),
                    "href": element.get_attribute("href"),
                })
            except Exception:
                pass
        save_json("arrangement_elements.json", elements)

        logs = driver.get_log("performance")
        requests = []
        for entry in logs:
            try:
                message = json.loads(entry["message"])["message"]
                if message["method"] == "Network.requestWillBeSent":
                    request = message["params"]["request"]
                    url = request["url"]
                    if "qgjw.suet.edu.cn" in url:
                        requests.append({
                            "method": request["method"],
                            "url": url,
                            "postData": request.get("postData", ""),
                        })
            except Exception:
                pass
        save_json("network.json", requests)
        print(json.dumps({"ok": True, "url": driver.current_url, "files": len(list(OUT.iterdir()))}, ensure_ascii=False))
    finally:
        password = ""
        driver.quit()


if __name__ == "__main__":
    main()
