"""Data-zone runner (research/konzept-datenzonen.md §2/§5, guide docs/operations.md → Data-zone runner).

A deterministic service without an LLM on the TaskBrigade host (`taskbrigade-runner.service`, user `tbr-runner`,
started as `tbr-admin runner serve`). It claims `run`/`profile` tasks of its runner agent, prepares a job directory,
decrypts the input dataset into it, starts the sandbox unit `taskbrigade-job@<job>.service` (no network, only the
job directory, resource limits, stdout/stderr to files) and stores the outputs encrypted in
`TBR_DATA_DIR/quarantine` for the output gate.

Modules: `config` (runner.toml), `precheck` (static script check), `sandbox` (job directory and systemd unit),
`service` (claim loop and job flow); Docker (compose profile `data`): `docker` (launcher and job containers),
`container` (entry point of the runner image).
"""
