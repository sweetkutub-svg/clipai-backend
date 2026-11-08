"""Removes the duplicated Facebook block from main.py (keeps the newer one). Run inside the backend folder."""
import py_compile, shutil, sys
from pathlib import Path

p = Path("main.py")
raw = p.read_bytes()
text = raw.decode("utf-8-sig")
lines = text.splitlines(keepends=True)

MARK = "# ---- Facebook (Page Reels) ----"
marks = [i for i, l in enumerate(lines) if l.strip() == MARK]
if len(marks) < 2:
    print(f"Duplicate block not found (marker count = {len(marks)}). Nothing changed.")
    sys.exit(0)

second = marks[1]
wait = next((i for i in range(second, len(lines)) if lines[i].startswith("def fb_wait_ready(")), None)
if wait is None:
    print("Could not find 'def fb_wait_ready(' after the duplicate block. Nothing changed.")
    sys.exit(1)

new_lines = lines[:second] + lines[wait:]
new_text = "".join(new_lines)

# sanity checks before touching the file
checks = {
    "def load_fb": 1, "def upload_to_facebook_route": 1, "def fb_wait_ready": 1,
    "def finish_clip": 1, "def upload_to_all_route": 1, '@app.get("/auth/facebook/url")': 1,
}
for key, want in checks.items():
    got = new_text.count(key)
    if got != want:
        print(f"Check failed: '{key}' appears {got} times (expected {want}). Nothing changed.")
        sys.exit(1)
if '_gone = finish_clip(user.id, req, "facebook")' not in new_text:
    print("Check failed: the Facebook upload route does not call finish_clip. Nothing changed.")
    sys.exit(1)

tmp = Path("main_fixed_tmp.py")
tmp.write_text(new_text, encoding="utf-8", newline="")
try:
    py_compile.compile(str(tmp), doraise=True)
except py_compile.PyCompileError as e:
    tmp.unlink(missing_ok=True)
    print("Syntax check failed, nothing changed:", e)
    sys.exit(1)

shutil.copy("main.py", "main.py.before_fix")
tmp.replace(p)
print(f"Done. Removed {wait - second} duplicate lines. Backup saved as main.py.before_fix")
