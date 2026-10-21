"""Check the selected AMI input schema without importing its stateful service."""
import ast
from pathlib import Path


def validate_ami_roles(config, roles):
    path = Path(config["source_root"]) / "app/main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    allowed = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Message":
            for field in node.body:
                if (isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
                        and field.target.id == "role" and isinstance(field.annotation, ast.Subscript)):
                    annotation = field.annotation
                    name = getattr(annotation.value, "id", getattr(annotation.value, "attr", None))
                    if name == "Literal":
                        values = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
                        allowed = {v.value for v in values if isinstance(v, ast.Constant)}
    if allowed is None:
        raise ValueError(f"Cannot verify AMI Message.role schema in {path}")
    missing = set(roles) - allowed
    if missing:
        raise ValueError(f"Selected AMI source rejects roles {sorted(missing)}; select the system-role compatible source")
    return sorted(allowed)


def validate_method_inputs(prepared, methods):
    if "activememoryindex" not in methods:
        return
    from ..runtime.artifacts import read_json, rows
    roles = set()
    for case in rows(Path(prepared) / "cases.jsonl"):
        history = read_json(Path(prepared) / case["history"])
        roles.update(m["role"] for s in history["sessions"] for m in s["messages"])
    validate_ami_roles(methods["activememoryindex"], roles)
