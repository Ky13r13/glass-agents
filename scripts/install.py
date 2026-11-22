"""Build an executable standard-library zipapp; no pip, sudo or network needed."""
import argparse
from pathlib import Path
import os
import shutil
import sys
import tempfile
import zipapp


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("agentctl requires Python 3.11 or newer.")
    p = argparse.ArgumentParser()
    p.add_argument("--output", default=str(Path.home() / ".local/bin/agentctl"))
    p.add_argument("--replace", action="store_true", help="Replace an existing executable at the selected path.")
    args = p.parse_args()
    out = Path(args.output).expanduser().absolute()
    if out.exists() and not args.replace:
        raise SystemExit(f"{out} already exists. Choose --output or use --replace to update it.")
    out.parent.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="agentctl-install-") as temp:
        staging = Path(temp)
        shutil.copytree(root / "agentctl", staging / "agentctl", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (staging / "__main__.py").write_text("from agentctl.cli import main\nraise SystemExit(main())\n")
        fd, temporary = tempfile.mkstemp(prefix=".agentctl-install-", dir=out.parent)
        os.close(fd)
        try:
            zipapp.create_archive(staging, temporary, interpreter="/usr/bin/env python3", compressed=True)
            os.chmod(temporary, 0o755)
            os.replace(temporary, out)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(f"Installed {out}\nRun: {out} --help")
    if str(out.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print('Add ~/.local/bin to PATH, for example: export PATH="$HOME/.local/bin:$PATH"')


if __name__ == "__main__":
    main()
