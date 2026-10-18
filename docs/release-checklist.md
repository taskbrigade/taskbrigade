<img src="../app/static/brand/logo@2x.png" alt="TaskBrigade" height="36">

# Public release checklist

The internal Gitea repository remains the working copy. Its history must never be mirrored or pushed to GitHub. Publish only the new repository produced by `deploy/release/export-public.sh`.

- [ ] Raise the version before every rollout or release, also for internal deployments: `version` in
  `pyproject.toml`, `uv lock` (updates `uv.lock`), rename `## Unreleased` in `CHANGELOG.md` to `## <version> – <date>`.
  Agents update with `tbr self-update` when the version **or** the build identifier of the server's wheel differs
  (`tbr version`, `/healthz`: `<version>+<build>`); the build check is a safety net, not a replacement for the bump.
- [ ] Regenerate the README screenshots when the UI changed: `python3 deploy/release/screenshots.py`
  (Docker demo with invented data, [docs/screenshots](screenshots/README.md)); look at every image before committing.
- [ ] Confirm the release content and third-party notices are ready. Check `LICENSE`, package metadata, bundled asset licenses, and attribution.
- [ ] Check the security contact address in `SECURITY.md` (security@taskbrigade.dev) and that GitHub private vulnerability reporting is enabled.
- [ ] Internal material stays in the working repository but is never exported: `EXCLUDED_PATHS` in
  `deploy/release/export-public.sh` (`research/`, `docs/naming/`, `media/`, `mediaassets/ChatGPT-Bild*.png`,
  `Marketing/`, `Produktvideo/`, `docs/ui-redesign/` (redesign mockups and review screenshots),
  `tasks/` – attachments of TaskBrigade's own tasks). Add new internal folders there before the export.
- [ ] Commit the intended source state. The export uses `HEAD` and stops when tracked files have staged or unstaged changes. Untracked files are excluded.
- [ ] Install `gitleaks` and review its current rules. Prepare a **local, untracked** UTF-8 patterns file with one Python regular expression per line for any organization-specific hostnames, domains, IDs, or other private markers. Do not put actual private values in this repository. The script needs gitleaks 8.19 or newer (`gitleaks dir <path>`).
- [ ] Export only after every open task's `brain-changes/` and `changelog.d/` files have been applied: a pending
  change file quotes the old `brain.md` text, which the patterns file is meant to catch.
- [ ] Probe run before the real export: in a scratch clone of the release commit run the export with gitleaks and
  your patterns file, then `git -C <output> grep -n -I -E -f <patterns file>` (no output expected). gitleaks
  findings in test fixtures are marked with `# gitleaks:allow` on the line; rephrase prose that gitleaks flags.
- [ ] Run the export into a path that does not yet exist:

  ```sh
  deploy/release/export-public.sh \
    --source . \
    --output ../taskbrigade-public \
    --author-name "Public Release" \
    --author-email "release@example.com" \
    --patterns-file /path/to/local-private-patterns.txt
  ```

- [ ] Confirm the secret scan and internal-pattern scan succeeded. The script fails before creating the destination if either scan reports a finding. Inspect the exported files manually, including documentation, sample configuration, and bundled assets. Search for private names and identifiers that automated rules may miss.
- [ ] In the exported repository, verify that there is exactly one commit and that both author and committer match the supplied public identity. Review the full file list and diff against the intended release content.
- [ ] Create a separate, sanitized release repository in Gitea from this export. Configure a push mirror **from that release repository to GitHub**, with a narrowly scoped credential. Never configure a mirror from the internal working repository.
- [ ] Set the public version tag on the sanitized release repository and verify that both the branch and tag arrive on GitHub. Check the GitHub commit history, author details, license display, and public clone before announcing the release.

- [ ] A tag `v*` makes the `release` job push `ghcr.io/taskbrigade/taskbrigade` (`IMAGE` in
  `.github/workflows/ci.yml`) for amd64 and arm64 once all other jobs are green ([CI](ci.md)); make the GHCR package
  public after the first push.

The export script creates a local repository only. It does not push, mirror, or tag.
