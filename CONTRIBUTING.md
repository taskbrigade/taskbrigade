# Contributing to TaskBrigade

TaskBrigade coordinates humans and AI agents: assignment, leases, review, and merge.
Documentation and commit messages for new work are in English. Docker is not required
to set up the project or to run the unit tests.

By contributing, you agree that your contributions are licensed under the Apache License
2.0 (see `LICENSE`).

## Requirements

- Python 3.12 (see `.python-version`; the project requires `>=3.12,<3.13`)
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
git clone https://github.com/taskbrigade/taskbrigade.git
cd taskbrigade
uv sync
```

The GitHub URL above is a placeholder until the public repository exists.

Run the app locally (state stays in this checkout; nothing is written to `/var/lib/taskbrigade`):

```bash
export TBR_STATE_DIR="$PWD/var"
export SECRET_KEY=dev
export BOOTSTRAP_ADMIN_PASSWORD_HASH="$(uv run tbr-admin hash-password)"
uv run tbr-admin init-key
uv run uvicorn app.main:app --port 8090
```

Then sign in as `admin` and configure the database under Admin → Database.

## Tests

Unit tests do not need a database. Database tests are skipped unless a test database is configured.

```bash
uv run pytest
```

Database tests (`pytest -m db`, for example `tests/test_ui_cli.py`) must run only against a database whose name ends with `_test`. Never point them at a production database.

Optional local MariaDB (Docker is only for this extra check, not for normal setup):

```bash
docker run -d --rm --name tm-test-mariadb \
  -e MARIADB_ROOT_PASSWORD=root \
  -e MARIADB_DATABASE=tm_test \
  -e MARIADB_USER=tm \
  -e MARIADB_PASSWORD=tm-test-password \
  -p 127.0.0.1:33306:3306 \
  mariadb:10.5
```

```bash
TBR_TEST_DATABASE_URL="mysql+pymysql://tm:tm-test-password@127.0.0.1:33306/tm_test?charset=utf8mb4" \
  uv run pytest -m db
```

Use a password that does not appear inside a `/tmp/tm-db-*.cnf` path. `test_backup` checks that.

Live Gitea tests are optional and create a throwaway repository:

```bash
TBR_TEST_GITEA_TOKEN=... TBR_TEST_GITEA_COLLABORATOR=... uv run pytest -m gitea_live
```

## Translations

The web UI is English by default and also available in German. English source strings are the message ids;
translations live in `app/locales/<lang>.json` (`app/i18n.py` explains the format, plural forms included).

- Templates: `{{ _("Save") }}`, `{{ _("Log out %(name)s", name=user.name) }}`,
  `{{ ngettext("%(num)d task", "%(num)d tasks", total) }}`. Jinja formats the string itself, so write a literal
  `%` as `%%`.
- Python: `from app.i18n import gettext as _, ngettext, N_`. Use `N_("…")` for strings defined at import time
  (labels, dicts) and translate them with `_()` where they are shown.
- Language per request: cookie `tm_lang` (switcher in the top bar), otherwise the admin setting `ui_language`,
  otherwise `UI_LANGUAGE` from `.env`, otherwise English.
- `tests/test_i18n.py` extracts all strings from templates and Python code, fails on missing or stale catalog
  entries and renders every template in every language. The other tests run with `UI_LANGUAGE=de`.
- A new language needs `app/locales/<lang>.json`, an entry in `LANGUAGES`, `PLURAL_RULES`, `DATE_FORMATS` and
  `DECIMAL_SEPARATOR` in `app/i18n.py`.

## Branches and pull requests

1. Branch from `main`. Do not push directly to `main`.
2. Name the branch `task/<TASK-ID>-short-english-slug` when the change belongs to a TaskBrigade task. Otherwise use `feat/`, `fix/`, or `docs/` plus a short slug.
3. Open a pull request against `main`.
4. Describe what changed and why. Link the related issue when there is one.
5. `uv run pytest` must be green. If you run database tests, say which `*_test` database you used.
   `uvx ruff@0.14.0 check .` must pass as well; CI runs both, plus an image build and a Compose smoke test
   ([docs/ci.md](docs/ci.md)).
6. Do not include secrets, tokens, or passwords.

The internal working copy may live on a private Gitea instance. Public review happens on GitHub once that repository exists.

## Commit messages

Write commit messages in English, in the imperative mood.

- Subject: about 72 characters, no trailing period. Prefix a task id when you have one, for example `[DEMO-0042] Add the Apache-2.0 license`.
- Body: explain why, not a list of files. Separate it from the subject with a blank line.
- Existing history is partly in German. New commits stay in English.
