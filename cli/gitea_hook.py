"""Administrator commands for an explicitly mounted native Gitea repository root."""
from pathlib import Path

import typer

from app.services import gitea_hook
from cli.i18n import _


def register(data_app, data_db):
    hook_app = typer.Typer(help=_("Install or remove Gitea data-zone push guards"), no_args_is_help=True)
    data_app.add_typer(hook_app, name="gitea-hook")

    def apply(action, repo_root, dry_run=False):
        failed = False
        with data_db().session() as session:
            projects = gitea_hook.repositories(session, protected_only=action != "uninstall")
            for project in projects:
                label = f"{project.gitea_owner}/{project.gitea_repo}"
                try:
                    path = gitea_hook.hook_path(repo_root, project.gitea_owner, project.gitea_repo)
                    if action == "status":
                        state = gitea_hook.status(path)
                        typer.echo(f"{label}: {state}")
                        failed |= state != "installed"
                    else:
                        getattr(gitea_hook, action)(path, dry_run=dry_run)
                        typer.echo(_("%(repo)s: %(action)s%(note)s", repo=label, action=action,
                                     note=_(" (dry run)") if dry_run else ""))
                except (OSError, ValueError, UnicodeError) as exc:
                    typer.echo(_("%(repo)s: hook operation failed: %(error)s", repo=label, error=exc), err=True)
                    failed = True
        if not projects:
            typer.echo(_("No matching Gitea repositories."))
        if failed:
            raise typer.Exit(1)

    @hook_app.command("install", help=_("Install/update guards in data-zone repositories; run again after project changes"))
    def install(repo_root: Path = typer.Option(..., exists=True, file_okay=False),
                dry_run: bool = typer.Option(False)):
        apply("install", repo_root, dry_run)

    @hook_app.command("uninstall", help=_("Remove managed guards from all mapped repositories, including former data zones"))
    def uninstall(repo_root: Path = typer.Option(..., exists=True, file_okay=False),
                  dry_run: bool = typer.Option(False)):
        apply("uninstall", repo_root, dry_run)

    @hook_app.command("status", help=_("Report missing/outdated guards; exit 1 if any repository is unprotected"))
    def status(repo_root: Path = typer.Option(..., exists=True, file_okay=False)):
        apply("status", repo_root)

    @hook_app.command("payload", help=_("Print the standalone hook (no database access)"))
    def payload():
        typer.echo(gitea_hook.payload(), nl=False)
