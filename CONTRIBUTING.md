# Contributing

Issues and pull requests are welcome.

1. Fork the repository and create a focused branch.
2. Install the development dependencies with
   `python -m pip install -e '.[dev]'`.
3. Add or update tests for behavioral changes.
4. Run:

   ```bash
   ruff check .
   ruff format --check .
   mypy
   pytest --cov --cov-report=term-missing
   pip-audit
   ```

5. Describe the user impact, security implications, and validation in the pull
   request.

Never commit credentials, Telegram updates from real users, checkout URLs, or
production webhook payloads. By contributing, you agree that your contribution
is licensed under the MIT License.

