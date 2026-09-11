"""Allow ``python -m lmls`` as well as the installed ``lmls`` script."""

from .cli.__main__ import app

if __name__ == "__main__":
    app()
