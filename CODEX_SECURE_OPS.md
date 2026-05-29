# Codex Secure Ops Notes

## Purpose
- This file records secret-adjacent operational memory for the workspace.
- It must never contain raw secrets, private keys, bearer tokens, passwords, or cookie material.
- It may record locations, dependency points, validation checks, and operational rules.

## Non-Negotiable Rules
- Do not paste SSH private keys into this repository.
- Do not paste GitHub bearer tokens, API tokens, or remote `.env` contents into this repository.
- Do not echo secret values into `CODEX_DEV_STABLE.md`, `CODEX_DEV_UPDATES.md`, or normal user-facing output.
- If a future session discovers an actual secret committed here, stop and rotate it instead of documenting it.

## Secret Storage Boundaries
- Machine-local Python/runtime secrets belong in `src\ashare\engine\local_settings.py` or the local environment, not in tracked docs.
- SSH key material belongs in the operator machine SSH store such as `%USERPROFILE%\.ssh\`.
- GitHub publishing credentials should be resolved through Git Credential Manager or equivalent local credential storage, not committed files.
- Remote service secrets for deployment scripts belong in remote host env files such as `/etc/ashare_portal_backend.env` or `/etc/ashare_operator_chat_backend.env`, not in this repo.

## SSH-Dependent Scripts
- These scripts assume working local `ssh` / `scp` access to the remote host and should be treated as SSH-dependent operations:
  - `scripts\deploy_operator_chat_backend_to_server.ps1`
  - `scripts\deploy_portal_backend_to_server.ps1`
  - `scripts\deploy_remote_clock_worker_to_server.ps1`
  - `scripts\publish_audit_report_to_site.ps1`
  - `scripts\publish_operator_runtime_context_to_site.ps1`
  - `scripts\start_operator_ollama_reverse_tunnel.ps1`
  - `scripts\stop_operator_ollama_reverse_tunnel.ps1`
- Current default remote deployment target embedded in several scripts is:
  - user: `ubuntu`
  - host: `43.129.28.141`
- That host/user pair is operational metadata, not a secret, but future sessions should still avoid copying connection details into casual output unless needed.

## Credential-Dependent Scripts
- `scripts\publish_csharp_runtime_skeleton_repo.ps1` depends on Git Credential Manager and uses a bearer token returned by `git credential-manager get`.
- `src\ashare\engine\local_settings.py` and related config builders may supply vendor/API tokens such as Tushare; document the dependency, not the value.

## SSH Readiness Checklist
- Confirm `ssh` is installed and on `PATH`.
- Confirm the expected private key is present locally and readable by the operator account.
- Confirm the remote host fingerprint is already trusted in `known_hosts` or be prepared for first-connect handling outside automated scripts.
- Confirm the remote user has permission to write the target directories referenced by the deployment script.
- Confirm `scp` works before running deployment scripts that move bundles or env files.

## Git Credential Readiness Checklist
- Confirm `git credential-manager get` works on the operator machine before running publish scripts.
- Confirm the resolved credential has the repository/API scope needed by the script.
- Never print the resolved password/token field to logs or chat output.

## Remote Env/File Expectations
- Portal backend deploy script writes `/etc/ashare_portal_backend.env`.
- Operator chat backend deploy script writes `/etc/ashare_operator_chat_backend.env`.
- Remote systemd unit files are installed under `/etc/systemd/system/`.
- Remote deployment scripts also patch nginx and reload services; SSH readiness alone is not enough.

## Documentation Rule
- Stable operational truth should link to this file, not duplicate sensitive setup detail inline.
- If SSH or credential workflows materially change, update this file together with:
  - `CODEX_DEV_STABLE.md`
  - `CODEX_DEV_UPDATES.md`
  - `CODEX_DEV_LOG_INDEX.md`
