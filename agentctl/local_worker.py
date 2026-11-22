"""Isolated HTTP worker so the parent can bound and cancel local requests."""
import json
import sys

from .local import Ollama


def main():
    request = json.load(sys.stdin)
    response = Ollama(request["config"]).chat(request["prompt"], model=request.get("model"))
    print(json.dumps({"type": "local.response", "response": response}), flush=True)


if __name__ == "__main__":
    main()
