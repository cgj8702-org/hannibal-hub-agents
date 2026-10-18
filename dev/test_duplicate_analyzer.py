import ast
import difflib
from pathlib import Path


def sk(node):
    t = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant):
            t.append("C")
        elif isinstance(n, ast.Name):
            t.append("N")
        elif isinstance(n, ast.Attribute):
            t.append("A:" + n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            t.append("F:" + n.name)
        elif isinstance(n, ast.Call):
            t.append("CALL")
        elif isinstance(n, (ast.Compare, ast.BoolOp, ast.Assert)):
            t.append(type(n).__name__)
    return t


cands = []
for f in sorted(Path("tests").rglob("*.py")):
    try:
        tree = ast.parse(f.read_text())
    except SyntaxError:
        continue
    owner = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            for sub in n.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner[(sub.name, sub.lineno)] = n.name
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nm = n.name
        if not nm.startswith("test_") or nm.startswith(("__", "visit_")) or n.decorator_list:
            continue
        cls = owner.get((nm, n.lineno))
        cands.append(
            {
                "name": nm,
                "file": str(f),
                "line": n.lineno,
                "end": n.end_lineno,
                "loc": n.end_lineno - n.lineno + 1,
                "cls": cls,
                "sig": sk(n),
            }
        )

used = set()
families = []
for i, c1 in enumerate(cands):
    if id(c1) in used:
        continue
    fam = [c1]
    used.add(id(c1))
    s1 = c1["sig"]
    l1 = len(s1)
    for c2 in cands[i + 1 :]:
        if id(c2) in used:
            continue
        s2 = c2["sig"]
        l2 = len(s2)
        if 2 * min(l1, l2) / (l1 + l2) <= 0.90:
            continue
        r = difflib.SequenceMatcher(None, s1, s2).ratio()
        if r > 0.90:
            fam.append(c2)
            used.add(id(c2))
    if len(fam) > 1:
        families.append(fam)

families.sort(key=len, reverse=True)

print(f"Total test functions: {len(cands)}")
print(f"Duplicate families (>0.90): {len(families)}")
print(f"Tests in those families: {sum(len(f) for f in families)}")
print(
    f"If collapsed to 1/case-set: {len(cands) - sum(len(f) for f in families) + len(families)}  (saves {sum(len(f) - 1 for f in families)} funcs)"
)
print(f"Largest families: {sorted((len(f) for f in families), reverse=True)[:6]}")
