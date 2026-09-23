#!/usr/bin/env python3
"""Read a teacher's published workload source data from qgjw.suet.edu.cn.

Credentials are accepted through stdin and are never written to disk or stdout.
"""
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from html.parser import HTMLParser

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import Select
    from selenium.webdriver.support.ui import WebDriverWait
except ModuleNotFoundError:  # Parsing and workbook tests do not need a browser.
    webdriver = Options = By = EC = Select = WebDriverWait = None


BASE = "https://qgjw.suet.edu.cn"


def configure_stdio():
    """Keep the frozen Windows connector and its parent process on UTF-8 pipes."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure=getattr(stream,"reconfigure",None)
        if reconfigure:
            reconfigure(encoding="utf-8",errors="replace")


def chrome_start_error(exc):
    detail=re.sub(r"\s+"," ",str(exc)).strip()
    hint="无法启动 Chrome 登录组件。请确认已安装 Google Chrome；首次运行还需联网获取与 Chrome 匹配的驱动"
    return RuntimeError(f"{hint}。{detail[:400]}" if detail else hint)

# Teaching week 1 is shared by every teacher. Keep confirmed school-calendar
# dates here instead of guessing from the first Monday in March/September.
TERM_STARTS = {
    (2024, 0): date(2024, 9, 2),
    (2024, 1): date(2025, 2, 24),
    (2025, 0): date(2025, 9, 1),
    (2025, 1): date(2026, 3, 9),
    (2026, 0): date(2026, 8, 31),
}

def progress(percent, message):
    print("PROGRESS " + json.dumps({"percent": percent, "message": message}, ensure_ascii=False), file=sys.stderr, flush=True)


class Rows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.rows=[]; self.row=None; self.cell=None
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.row=[]
        elif tag in ("td", "th") and self.row is not None: self.cell=[]
        elif tag == "br" and self.cell is not None: self.cell.append(" ")
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip()); self.cell=None
        elif tag == "tr" and self.row is not None:
            if self.row: self.rows.append(self.row)
            self.row=None


class TextBlocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.stack=[]; self.blocks=[]
    def handle_starttag(self, tag, attrs):
        if tag in ("div", "li"): self.stack.append([tag, []])
        elif tag == "br":
            for item in self.stack: item[1].append("\n")
    def handle_data(self, data):
        for item in self.stack: item[1].append(data)
    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            _, data=self.stack.pop(); raw="".join(data)
            text="\n".join(re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.split("\n") if line.strip())
            if text: self.blocks.append(text)


class ScheduleDivs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.current=None; self.blocks=[]
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=="div" and re.fullmatch(r"0[1-7][1-5]",attrs.get("id", "")):
            self.current=[attrs["id"],[]]
        elif tag=="br" and self.current: self.current[1].append("\n")
    def handle_data(self,data):
        if self.current: self.current[1].append(data)
    def handle_endtag(self,tag):
        if tag=="div" and self.current:
            self.blocks.append((self.current[0],"".join(self.current[1]))); self.current=None


def table_rows(html):
    parser=Rows(); parser.feed(html); return parser.rows


def semester_key(year, term):
    return f"{year}-{year+1}-{1 if int(term)==0 else 2}"


def parse_home_identity(html):
    match=re.search(r"\[([^]]+)\]\s*([^<\s]+)\s*</label>",html or "")
    return (match.group(1).strip(),match.group(2).strip()) if match else ("","")


def num(value, default=0.0):
    try: return float(re.search(r"-?\d+(?:\.\d+)?", str(value)).group())
    except Exception: return default


def parse_theory(html, semester, employee_id, employee_name):
    items=[]; repeats=Counter()
    for row in table_rows(html):
        if len(row) < 19 or not row[0].isdigit() or not re.search(r"\[[^]]+\]", row[2]): continue
        match=re.match(r"\[([^]]+)\](.*)", row[2]); code, course=match.group(1).strip(), match.group(2).strip()
        people=num(row[17]); class_name=row[18].strip(); total=num(row[4]); lecture=num(row[5])
        nonlecture=max(0.0, total-lecture)
        repeat_key=(code, course); repeat=1.0 if repeats[repeat_key] == 0 else .9; repeats[repeat_key]+=1
        category=1.2 if ("本" in class_name or "专升本" in class_name) else 1.0
        items.append({"kind":"theory","employee_id":employee_id,"employee_name":employee_name,"semester":semester,
          "course":course,"class_name":class_name,"course_code":code,"student_count":people,"total_hours":total,
          "experiment_hours":nonlecture,"experiment_students":max(50,min(75,people)),"category_coeff":category,
          "repeat_coeff":repeat,"course_coeff":1,"online_coeff":1,"practice_coeff":0,"weeks":0,
          "instructors":1,"enterprise_coeff":1,"manual_workload":0,"source":"平台:承担理论课程"})
    return items


def parse_practice(html, semester, employee_id, employee_name):
    items=[]
    for row in table_rows(html):
        if len(row) < 7 or not row[0].isdigit(): continue
        match=re.match(r"\[([^]]+)\](.*)", row[1]);
        if not match: continue
        code, course=match.group(1).strip(),match.group(2).strip(); category=row[2]; weeks=num(row[4]); people=num(row[6]); cls=row[5]
        kind="training" if any(x in category+course for x in ("技能","实训","实战","认识实习")) else "internship" if "实习" in course else "thesis" if any(x in course for x in ("毕业设计","毕业论文")) else "manual"
        coeff=.5 if kind=="training" else (4 if ("本" in cls or "专升本" in cls) else 3) if kind=="internship" else (6 if "本" in cls else .3) if kind=="thesis" else 0
        hours=(weeks*10 if kind=="training" else weeks*people*.5 if kind=="internship"
               else people*(6 if coeff>=1 else 3) if kind=="thesis" else 0)
        items.append({"kind":kind,"employee_id":employee_id,"employee_name":employee_name,"semester":semester,"course":course,
          "class_name":cls,"course_code":code,"student_count":people,"total_hours":hours,"experiment_hours":0,
          "experiment_students":0,"category_coeff":1,"repeat_coeff":1,"course_coeff":1,"online_coeff":1,
          "practice_coeff":coeff,"weeks":weeks,"instructors":1,"enterprise_coeff":1,"manual_workload":0,
          "source":"平台:指导实践环节"})
    return items


def week_numbers(spec, parity=""):
    result=[]
    for part in spec.replace("周","").split(","):
        part=part.strip()
        if not part: continue
        if "-" in part:
            a,b=part.split("-",1); result.extend(range(int(a),int(b)+1))
        elif part.isdigit(): result.append(int(part))
    if "单" in parity: result=[x for x in result if x%2]
    if "双" in parity: result=[x for x in result if not x%2]
    return sorted(set(result))


def first_monday(year, term):
    key=(int(year),int(term))
    if key in TERM_STARTS: return TERM_STARTS[key]
    start=date(year,9,1) if int(term)==0 else date(year+1,3,1)
    # Fall terms can start in the final days of August. Unknown terms retain a
    # deterministic calendar fallback until their official date is added.
    return start-timedelta(days=start.weekday()) if int(term)==0 else start+timedelta(days=(7-start.weekday())%7)


def month_bucket(year, term, week):
    d=first_monday(year,term)+timedelta(days=(week-1)*7)
    return month_key_for_date(d, term)


def month_key_for_date(value, term):
    if int(term)==0: return "9月" if value.month in (8,9) else "10月" if value.month==10 else "11月" if value.month==11 else "12-1月"
    return "3月" if value.month in (2,3) else "4月" if value.month==4 else "5月" if value.month==5 else "6-7月"


def parse_schedule_hours(html, year, term):
    parser=TextBlocks(); parser.feed(html); monthly=defaultdict(lambda:[0.0,0.0]); notes=[]
    seen=set()
    lines=[line.strip() for block in parser.blocks for line in block.splitlines() if line.strip() and len(line.strip()) < 500]
    for text in lines:
        if text.startswith("注") and "周" in text: notes.append(text)
        if "周" not in text or "节" not in text: continue
        match=re.search(r"\[([^]]+)\]周\s*(单周|双周)?\s*(\d+)-(\d+)节",text)
        if not match: continue
        signature=(text,match.group(0))
        if signature in seen: continue
        seen.add(signature)
        weeks=week_numbers(match.group(1),match.group(2) or ""); periods=int(match.group(4))-int(match.group(3))+1
        practical=any(k in text for k in ("实验室","机房","实训室","计算中心","算力中心"))
        for week in weeks: monthly[month_bucket(year,term,week)][1 if practical else 0]+=periods
    return monthly, notes


def parse_schedule_theory(html, semester, employee_id, employee_name):
    parser=TextBlocks(); parser.feed(html); grouped={}; seen=set()
    lines=[line.strip() for block in parser.blocks for line in block.splitlines() if line.strip() and len(line.strip()) < 500]
    for text in lines:
        match=re.search(r"\[([^]]+)\]周\s*(单周|双周)?\s*(\d+)-(\d+)节",text)
        tail=re.search(r"节\s+(\d+)\s+(.+?)\s+本部（中心校区）\s+(.+)$",text)
        exam=re.search(r"\s(考试|考查)\s",text)
        if not match or not tail or not exam: continue
        signature=(text,match.group(0))
        if signature in seen: continue
        seen.add(signature)
        prefix=text[:exam.start()].strip()
        course=re.sub(r"(?:\s+\d+(?:\.\d+)?){1,7}$","",prefix).strip()
        class_name=tail.group(3).strip(); people=num(tail.group(1)); periods=int(match.group(4))-int(match.group(3))+1
        hours=len(week_numbers(match.group(1),match.group(2) or ""))*periods
        key=(course,class_name); record=grouped.setdefault(key,{"hours":0.0,"experiment":0.0,"people":people})
        record["hours"]+=hours; record["people"]=max(record["people"],people)
        if any(k in tail.group(2) for k in ("实验室","机房","实训室","计算中心","算力中心")): record["experiment"]+=hours
    items=[]; repeats=Counter()
    for (course,class_name),data in grouped.items():
        repeat=1.0 if repeats[course]==0 else .9; repeats[course]+=1; people=data["people"]
        items.append({"kind":"theory","employee_id":employee_id,"employee_name":employee_name,"semester":semester,"course":course,
          "class_name":class_name,"course_code":"","student_count":people,"total_hours":data["hours"],"experiment_hours":data["experiment"],
          "experiment_students":max(50,min(75,people)),"category_coeff":1.2 if ("本" in class_name or "专升本" in class_name) else 1,
          "repeat_coeff":repeat,"course_coeff":1,"online_coeff":1,"practice_coeff":0,"weeks":0,"instructors":1,
          "enterprise_coeff":1,"manual_workload":0,"source":"平台:教学安排"})
    return items


def schedule_line(text):
    match=re.search(r"\[([^]]+)\]周\s*(单周|双周)?\s*(\d+)-(\d+)节",text)
    tail=re.search(r"节\s+(\d+)\s+(.+?)\s+本部（中心校区）\s+(.+)$",text)
    exam=re.search(r"\s(考试|考查)\s",text)
    if not match or not tail or not exam: return None
    prefix=text[:exam.start()].strip(); course=re.sub(r"(?:\s+\d+(?:\.\d+)?){1,7}$","",prefix).strip()
    return {"course":course,"class_name":tail.group(3).strip(),"people":num(tail.group(1)),"room":tail.group(2),
      "weeks":week_numbers(match.group(1),match.group(2) or ""),"periods":f"{match.group(3)}-{match.group(4)}",
      "hours":int(match.group(4))-int(match.group(3))+1}


def parse_schedule_events(html, year, term, employee_id, employee_name):
    parser=ScheduleDivs(); parser.feed(html); events=[]; seen=set(); semester=semester_key(year,term); start=first_monday(year,term)
    for div_id,raw in parser.blocks:
        weekday=int(div_id[1])
        for raw_line in raw.splitlines():
            text=re.sub(r"\s+"," ",raw_line).strip(); data=schedule_line(text)
            if not data: continue
            practical=any(k in data["room"] for k in ("实验室","机房","实训室","计算中心","算力中心"))
            for week in data["weeks"]:
                event_date=start+timedelta(days=(week-1)*7+weekday-1)
                key=(week,weekday,data["course"],data["class_name"],data["periods"])
                if key in seen: continue
                seen.add(key); events.append({"semester":semester,"employee_id":employee_id,"employee_name":employee_name,
                  "event_date":event_date.isoformat(),"academic_week":week,"weekday":weekday,"course":data["course"],
                  "class_name":data["class_name"],"student_count":data["people"],"periods":data["periods"],
                  "hours":data["hours"],"category":"practice" if practical else "theory","source":"平台:教学安排"})
    # Notes identify concentrated-practice projects and weeks, but do not give
    # daily periods. They remain workload items via parse_note_items(); creating
    # five synthetic "1-6" days here would make the personal-hours sheet false.
    return events


def parse_note_items(notes, semester, employee_id, employee_name):
    items=[]; seen=set()
    for text in notes:
        clean=re.sub(r"^注\d+：", "", text).strip()
        match=re.search(r"(?:\[([^]]+)\])?(.*?)、\[([^]]+)周\]、(.+?)\(第.*?(\d+)人\)",clean)
        if not match: continue
        code=(match.group(1) or "").strip(); course=match.group(2).strip(); weeks=len(week_numbers(match.group(3))); cls=match.group(4).strip(); people=num(match.group(5))
        key=(code,course,cls)
        if key in seen: continue
        seen.add(key)
        kind="internship" if "岗位实习" in course else "thesis" if any(k in course for k in ("毕业设计","毕业论文")) else "training"
        coeff=.5 if kind=="training" else (4 if ("本" in cls or "专升本" in cls) else 3) if kind=="internship" else (6 if "本" in cls or "专升本" in cls else .3)
        hours=(weeks*10 if kind=="training" else weeks*people*.5 if kind=="internship"
               else people*(6 if coeff>=1 else 3) if kind=="thesis" else 0)
        items.append({"kind":kind,"employee_id":employee_id,"employee_name":employee_name,"semester":semester,"course":course,"class_name":cls,
          "course_code":code,"student_count":people,"total_hours":hours,"experiment_hours":0,"experiment_students":0,
          "category_coeff":1,"repeat_coeff":1,"course_coeff":1,"online_coeff":1,"practice_coeff":coeff,"weeks":weeks,
          "instructors":1,"enterprise_coeff":1,"manual_workload":0,"source":"平台:教学安排备注"})
    return items


def login(driver, username, password):
    driver.get(BASE+"/cas/login.action")
    wait=WebDriverWait(driver,12)
    wait.until(EC.presence_of_element_located((By.ID,"username")))
    driver.find_element(By.ID,"username1").click(); driver.find_element(By.ID,"username").send_keys(username)
    driver.find_element(By.ID,"password1").click(); driver.find_element(By.ID,"password").send_keys(password)
    driver.find_element(By.ID,"login").click()
    try: wait.until(lambda current: "/cas/" not in current.current_url)
    except Exception: raise RuntimeError("平台登录失败，请检查账号、密码或验证码")


def semester_options(driver, timeout=12):
    """Wait until the asynchronously populated semester selector has real values."""
    def read_options(current):
        try:
            values=[(option.get_attribute("value"),option.text.strip()) for option in Select(current.find_element(By.ID,"xnxq")).options if option.get_attribute("value")]
            return values or False
        except Exception:
            return False
    return WebDriverWait(driver,timeout).until(read_options)


def open_schedule_page(driver):
    """Open the semester selector directly; retain the old homepage warm-up only as fallback."""
    url=BASE+"/wjstgdfw/jxap.jxapb.html?menucode=T20201"
    driver.get(url)
    try:
        return semester_options(driver,8)
    except Exception:
        driver.get(BASE+"/frame/homes.action")
        driver.execute_async_script("const d=arguments[arguments.length-1];fetch('/wjstgdfw/jxap.jxapb.html?menucode=T20201',{credentials:'same-origin'}).then(r=>d(r.status)).catch(e=>d(0));")
        driver.get(url)
        return semester_options(driver,15)


def read_report_frame(driver, frame_id="frmReport", expected_text="", timeout=20):
    """Wait for the report iframe body, not merely for the iframe element."""
    WebDriverWait(driver,timeout).until(EC.frame_to_be_available_and_switch_to_it((By.ID,frame_id)))
    expected=re.sub(r"\s+","",expected_text or "")
    def loaded(current):
        html=current.page_source
        compact=re.sub(r"\s+","",html)
        if len(html)<5000 or "教师：" not in html: return False
        if expected and expected not in compact: return False
        return html
    return WebDriverWait(driver,timeout).until(loaded)


def extend_unique_practice(items, additions):
    """Merge practice sources without dropping thesis/internship notes or duplicating rows."""
    def key(item):
        return (item.get("semester",""),re.sub(r"\s+","",item.get("course","")).replace("课程实战","实战"),re.sub(r"\s+","",item.get("class_name","")))
    known={key(item) for item in items if item.get("kind")!="theory"}
    for item in additions:
        marker=key(item)
        if marker not in known:
            items.append(item); known.add(marker)


def main():
    configure_stdio()
    if webdriver is None:
        raise RuntimeError("缺少 Selenium，无法启动青果平台读取组件")
    request=json.loads(sys.stdin.readline()); username=str(request.get("username","")).strip(); password=str(request.get("password", ""))
    mode=str(request.get("mode") or "sync")
    requested_semester=str(request.get("semester") or "").strip()
    employee_id=str(request.get("employee_id") or username).strip(); employee_name=str(request.get("employee_name","")).strip()
    if not username or not password: raise RuntimeError("平台账号和密码不能为空")
    options=Options()
    chrome_binary=os.getenv("WORKCOUNT_CHROME_BINARY", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if os.path.isfile(chrome_binary): options.binary_location=chrome_binary
    options.page_load_strategy="eager"
    options.add_experimental_option("prefs",{"profile.managed_default_content_settings.images":2})
    for arg in ("--headless=new","--ignore-certificate-errors","--window-size=1440,1000","--disable-gpu","--no-sandbox"): options.add_argument(arg)
    if mode == "catalog": progress(5,"正在启动青果登录组件")
    try:
        driver=webdriver.Chrome(options=options)
    except Exception as exc:
        raise chrome_start_error(exc) from exc
    items=[]; monthly_rows=[]; schedule_events=[]
    try:
        if mode == "catalog": progress(20,"正在验证青果账号")
        login(driver,username,password); password=""
        home_id,home_name=parse_home_identity(driver.page_source)
        if home_id: employee_id=home_id
        if home_name: employee_name=home_name
        if mode == "auth":
            print(json.dumps({"employee_id":employee_id,"employee_name":employee_name or employee_id,"authenticated":True},ensure_ascii=False))
            return
        progress(55 if mode == "catalog" else 10,"账号验证成功，正在读取可用学期")
        available_options=open_schedule_page(driver)
        if mode == "catalog":
            progress(95,f"已读取 {len(available_options)} 个可用学期")
            print(json.dumps({"employee_id":employee_id,"employee_name":employee_name or employee_id,"college":"","semesters":[semester_key(*map(int,value.split(","))) for value,_ in available_options]},ensure_ascii=False))
            return
        semester_options=available_options
        if requested_semester:
            semester_options=[option for option in semester_options if semester_key(*map(int,option[0].split(",")))==requested_semester]
            if not semester_options: raise RuntimeError("平台没有该学期数据")
        schedules={}; inferred_name=""; college=""
        for option_index,(value,label) in enumerate(semester_options):
            progress(15+int(30*option_index/max(1,len(semester_options))),f"正在读取教学安排：{label}")
            Select(driver.find_element(By.ID,"xnxq")).select_by_value(value); driver.execute_script("arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",driver.find_element(By.ID,"xnxq"))
            driver.find_element(By.ID,"btnQry").click()
            try: html=read_report_frame(driver,expected_text=label)
            finally: driver.switch_to.default_content()
            y,t=map(int,value.split(",")); schedules[(y,t)]=html
            ident=re.search(r"教师：\[([^]]+)\]([^<\s]+)",html); dept=re.search(r"部门：([^<]+)",html)
            if ident: employee_id=ident.group(1).strip(); inferred_name=ident.group(2).strip()
            if dept: college=re.sub(r"\s+","",dept.group(1))
        if inferred_name: employee_name=inferred_name

        driver.get(BASE+"/frame/jw/teacherstudentmenu.jsp?menucode=T201"); time.sleep(3); driver.switch_to.frame("frame_1")
        terms=[(o.get_attribute("value"),o.text.strip()) for o in Select(driver.find_element(By.ID,"sel_xnxq")).options if o.get_attribute("value")]
        if requested_semester: terms=[option for option in terms if semester_key(*map(int,option[0].split(",")))==requested_semester]
        for term_index,(value,term_label) in enumerate(terms):
            progress(48+int(25*term_index/max(1,len(terms))),f"正在读取理论教学任务：{term_label}")
            Select(driver.find_element(By.ID,"sel_xnxq")).select_by_value(value); Select(driver.find_element(By.ID,"sel_xnxqh")).select_by_value(value)
            driver.find_element(By.ID,"btnQry").click()
            try: html=read_report_frame(driver,expected_text=term_label)
            finally: driver.switch_to.parent_frame()
            y,t=map(int,value.split(",")); items.extend(parse_theory(html,semester_key(y,t),employee_id,employee_name))
        driver.switch_to.default_content()

        progress(74,"正在读取实践教学环节")
        practice_current=[]
        try:
            year,term=next(iter(schedules))
            driver.get(BASE+"/frame/jw/teacherstudentmenu.jsp?menucode=T201")
            WebDriverWait(driver,12).until(lambda current: current.execute_script("return typeof switchMenu === 'function'"))
            try: driver.execute_script("switchMenu('2','T20102','指导实践环节')")
            except Exception: pass
            WebDriverWait(driver,12).until(EC.frame_to_be_available_and_switch_to_it((By.ID,"frame_2")))
            WebDriverWait(driver,12).until(EC.presence_of_element_located((By.ID,"ActionForm")))
            for field_id in ("xn","xn1","xq","sel_xnxq"):
                WebDriverWait(driver,12).until(EC.presence_of_element_located((By.ID,field_id)))
            value=f"{year},{term}"
            driver.execute_script(
                "document.getElementById('xn').value=arguments[0]; document.getElementById('xn1').value=arguments[1]; document.getElementById('xq').value=arguments[2]; const s=document.getElementById('sel_xnxq'); const v=arguments[0]+','+arguments[2]; if(![...s.options].some(o=>o.value===v)) s.add(new Option(v,v)); s.value=v; document.getElementById('ActionForm').action='./jxrw.zdhj_rpt.jsp?random='+Math.random(); document.getElementById('ActionForm').target='frmReport'; document.getElementById('ActionForm').submit();",
                str(year),str(year+1),str(term)
            )
            WebDriverWait(driver,15).until(EC.frame_to_be_available_and_switch_to_it((By.ID,"frmReport")))
            expected=f"{year}-{year+1}学年第{'一' if term==0 else '二'}学期"
            html=WebDriverWait(driver,15).until(lambda current: current.page_source if expected in re.sub(r"\s+","",current.page_source) else False)
            practice_current=parse_practice(html,semester_key(year,term),employee_id,employee_name)
        except Exception as exc:
            progress(76,f"实践环节报表未返回，将使用教学安排备注：{str(exc)[:80]}")
        finally: driver.switch_to.default_content()
        extend_unique_practice(items,practice_current)

        theory_semesters={x["semester"] for x in items if x["kind"]=="theory"}
        for schedule_index,((year,term),html) in enumerate(schedules.items()):
            progress(78+int(18*schedule_index/max(1,len(schedules))),f"正在计算第 {schedule_index+1}/{len(schedules)} 个学期")
            semester=semester_key(year,term); month_values,notes=parse_schedule_hours(html,year,term)
            if semester not in theory_semesters:
                items.extend(parse_schedule_theory(html,semester,employee_id,employee_name))
            term_events=parse_schedule_events(html,year,term,employee_id,employee_name); schedule_events.extend(term_events)
            event_monthly=defaultdict(lambda:[0.0,0.0])
            for event in term_events:
                d=date.fromisoformat(event["event_date"])
                key=month_key_for_date(d,term)
                event_monthly[key][1 if event["category"]=="practice" else 0]+=event["hours"]
            expected=("9月","10月","11月","12-1月") if term==0 else ("3月","4月","5月","6-7月")
            for key in expected:
                theory,practice=event_monthly[key]
                monthly_rows.append({"semester":semester,"employee_id":employee_id,"employee_name":employee_name,"college":college,"department":"大数据教研室","month_key":key,"theory_hours":theory,"practice_hours":practice})
            extend_unique_practice(items,parse_note_items(notes,semester,employee_id,employee_name))
        progress(98,"正在整理工作量和个人学时数据")
        print(json.dumps({"employee_id":employee_id,"employee_name":employee_name,"college":college,"items":items,"monthly_hours":monthly_rows,"schedule_events":schedule_events,"semesters":[semester_key(*x) for x in schedules]},ensure_ascii=False))
    finally:
        password=""; driver.quit()


if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(json.dumps({"error":str(exc)},ensure_ascii=False)); sys.exit(1)
