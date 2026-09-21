#!/usr/bin/env python3
"""Capture the authenticated teacher practical-task page and reports."""
import json
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


BASE = "https://qgjw.suet.edu.cn"
OUT = Path("/tmp/qgjw_practice")


def main():
    username = sys.stdin.readline().strip()
    password = sys.stdin.readline().strip()
    OUT.mkdir(parents=True, exist_ok=True)
    options = Options(); options.binary_location = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    for arg in ("--headless=new", "--ignore-certificate-errors", "--window-size=1440,1000", "--disable-gpu", "--no-sandbox"):
        options.add_argument(arg)
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    driver = webdriver.Chrome(options=options)
    try:
        driver.get(BASE + "/cas/login.action")
        driver.find_element(By.ID, "username1").click(); driver.find_element(By.ID, "username").send_keys(username)
        driver.find_element(By.ID, "password1").click(); driver.find_element(By.ID, "password").send_keys(password)
        driver.find_element(By.ID, "login").click()
        deadline=time.time()+25
        while time.time()<deadline and "/cas/" in driver.current_url: time.sleep(.5)
        if "/cas/" in driver.current_url: raise SystemExit("login failed")
        password=""; time.sleep(3)
        driver.get(BASE + "/frame/jw/teacherstudentmenu.jsp?menucode=T201"); time.sleep(3)
        try:
            driver.execute_script("switchMenu('2','T20102','指导实践环节')")
        except Exception:
            # The standalone parent page lacks a cosmetic banner callback; the
            # function schedules the iframe navigation before invoking it.
            pass
        time.sleep(4)
        driver.switch_to.frame("frame_2")
        (OUT / "page.html").write_text(driver.page_source)
        fields=[]
        for el in driver.find_elements(By.CSS_SELECTOR,"input,select"):
            fields.append({"tag":el.tag_name,"id":el.get_attribute("id"),"name":el.get_attribute("name"),"value":el.get_attribute("value"),"text":el.text.strip()})
        (OUT/"fields.json").write_text(json.dumps(fields,ensure_ascii=False,indent=2))
        reports=[]
        for year, term in ((2026,0),(2025,1),(2025,0),(2024,1),(2024,0)):
            driver.execute_script(
                "document.getElementById('xn').value=arguments[0]; document.getElementById('xn1').value=arguments[1]; document.getElementById('xq').value=arguments[2]; const s=document.getElementById('sel_xnxq'); const v=arguments[0]+','+arguments[2]; if(![...s.options].some(o=>o.value===v)) s.add(new Option(v,v)); s.value=v;",
                str(year), str(year+1), str(term)
            )
            driver.execute_script("document.getElementById('ActionForm').action='./jxrw.zdhj_rpt.jsp?random='+Math.random(); document.getElementById('ActionForm').target='frmReport'; document.getElementById('ActionForm').submit();")
            time.sleep(3)
            try:
                driver.switch_to.frame("frmReport")
                html=driver.page_source; name=f"report_{year}_{term}.html"; (OUT/name).write_text(html)
                reports.append({"year":year,"term":term,"size":len(html)})
                driver.switch_to.parent_frame()
            except Exception as exc:
                reports.append({"year":year,"term":term,"error":str(exc)}); driver.switch_to.parent_frame()
        requests=[]
        for entry in driver.get_log("performance"):
            try:
                msg=json.loads(entry["message"])["message"]
                if msg["method"]=="Network.requestWillBeSent":
                    req=msg["params"]["request"]; u=req["url"]
                    if any(k in u for k in ("jxrw.zdhj","droplist","sjjx")): requests.append({"method":req["method"],"url":u,"postData":req.get("postData","")})
            except Exception: pass
        (OUT/"network.json").write_text(json.dumps(requests,ensure_ascii=False,indent=2))
        print(json.dumps({"ok":True,"reports":reports,"files":[p.name for p in OUT.iterdir()]},ensure_ascii=False))
    finally:
        password=""; driver.quit()


if __name__ == "__main__": main()
