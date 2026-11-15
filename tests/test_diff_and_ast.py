from app.ast_analyzer import analyze_python
from app.diff_parser import added_lines, commentable_lines, number_patch

PATCH = """@@ -1,3 +1,4 @@
 import os
-x = 1
+x = 2
+y = 3
 print(x)"""


def test_commentable_and_added_lines():
    assert commentable_lines(PATCH) == {1, 2, 3, 4}
    assert added_lines(PATCH) == {2, 3}


def test_number_patch_marks_removed_lines_unnumbered():
    out = number_patch(PATCH).splitlines()
    assert out[1].strip() == "1  import os"
    assert out[2].strip().startswith("-x = 1")
    assert out[3].strip() == "2 +x = 2"


SOURCE = '''import os
import json
import subprocess

API_KEY = "sk-live-1234567890"

def handler(items=[]):
    try:
        eval(items[0])
    except:
        pass
    subprocess.run("ls " + items[1], shell=True)
    if items == None:
        return json.dumps(items)
'''


def _titles(findings):
    return {f.title for f in findings}


def test_ast_detects_common_issues():
    titles = _titles(analyze_python("svc/handler.py", SOURCE))
    assert "Possible hardcoded secret" in titles
    assert "Mutable default argument" in titles
    assert "Use of `eval()`" in titles
    assert "Bare `except:`" in titles
    assert "Shell command injection risk" in titles
    assert "Comparison to `None` with `==`" in titles
    assert "Unused import" in titles  # os


def test_ast_filters_to_changed_lines():
    findings = analyze_python("svc/handler.py", SOURCE, changed_lines={9})
    assert _titles(findings) == {"Use of `eval()`"}


def test_ast_reports_syntax_error():
    findings = analyze_python("bad.py", "def f(:\n  pass\n")
    assert findings[0].title == "Syntax error"
    assert findings[0].severity.value == "critical"


def test_secrets_ignored_in_tests():
    assert not analyze_python("tests/test_x.py", 'PASSWORD = "hunter2hunter2"\n')
