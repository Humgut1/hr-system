with open("C:/Users/lg/hr-system/app.py", "r", encoding="utf-8") as f:
    code = f.read()

# Add datetime import
if "from datetime import datetime" not in code:
    code = code.replace(
        "import os\nimport sqlite3\nfrom functools import wraps",
        "import os\nimport sqlite3\nfrom datetime import datetime, date\nfrom functools import wraps"
    )

with open("C:/Users/lg/hr-system/app.py", "w", encoding="utf-8") as f:
    f.write(code)
print("imports OK")
