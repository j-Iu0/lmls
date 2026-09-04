"""Allow ``python -m livesub`` as well as the installed ``livesub`` script."""

from .cli.__main__ import app

if __name__ == "__main__":
    app()
