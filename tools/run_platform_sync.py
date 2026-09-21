#!/usr/bin/env python3
"""One-shot local sync runner; credentials are read from stdin and not persisted."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def main():
    app.init_db()
    username = sys.stdin.readline().strip()
    password = sys.stdin.readline().rstrip("\r\n")
    employee_name = sys.stdin.readline().strip()
    result = app.sync_from_platform({
        "username": username,
        "password": password,
        "employee_id": username,
        "employee_name": employee_name,
    })
    password = ""
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
