"""Normalise touched files to the end-of-line style they have in HEAD.

The repository has no .gitattributes and mixed per-file EOLs, so a tool that rewrites a file with
platform line endings turns a three-line change into a whole-file diff. This script asks Git for the
HEAD bytes of each touched text file, decides CRLF vs LF from them, and rewrites the working copy in
that style. Nothing else about the file changes.
"""
import subprocess
import sys

FILES = subprocess.run(
    ["git", "diff", "--name-only"], capture_output=True, text=True, check=True
).stdout.split()

#: Files whose HEAD content is deliberately mixed (a section added in LF inside a CRLF file). A
#: whole-file conversion would rewrite hundreds of untouched lines, so these are left alone.
MIXED_EOL_IN_HEAD = {"app/backend/src/creatoros/db/enums.py"}

TEXT_SUFFIXES = (".py", ".ts", ".tsx", ".mjs", ".cjs", ".js", ".md", ".ps1", ".json", ".css", ".html", ".txt")

changed = []
for name in FILES:
    if not name.endswith(TEXT_SUFFIXES):
        continue
    if name in MIXED_EOL_IN_HEAD:
        print(f"skipping {name}: HEAD is mixed by section, edit it byte-wise instead")
        continue
    head = subprocess.run(["git", "show", f"HEAD:{name}"], capture_output=True)
    if head.returncode != 0:
        print(f"new file, leaving as-is: {name}")
        continue
    head_bytes = head.stdout
    crlf = head_bytes.count(b"\r\n")
    lf = head_bytes.count(b"\n") - crlf
    wanted = "crlf" if crlf > lf else "lf"

    with open(name, "rb") as handle:
        current = handle.read()
    normalised = current.replace(b"\r\n", b"\n")
    if wanted == "crlf":
        normalised = normalised.replace(b"\n", b"\r\n")

    if normalised != current:
        with open(name, "wb") as handle:
            handle.write(normalised)
        changed.append(f"{name} -> {wanted}")
    elif wanted == "crlf" and b"\r\n" not in current:
        print(f"warning: {name} is CRLF in HEAD but LF on disk")

print("\n".join(changed) or "no EOL changes needed")
sys.exit(0)
