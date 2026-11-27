# Fleet Analytics collector

**Know which instructions make your coding agents better.**

You tune `CLAUDE.md`, rules, skills, agents and prompts every week. Fleet Analytics
shows what each change actually did: cost, tokens, errors and interruptions per
instruction version, across your whole team.

- **Measure, don't guess.** Every instruction change becomes a version you can compare.
- **See the cost.** Tokens and spend per task, model and version.
- **Catch regressions.** Spot the edit that made agents slower, pricier or less reliable.
- **Zero friction.** Runs silently in your agents' hooks. No workflow change, no dependencies.
- **Private by default.** No prompts, answers or code leave your machine. Instruction
  text is only uploaded if you opt in.

Works with **Claude Code** (CLI, VS Code, other clients) and **GitHub Copilot** agent
sessions (VS Code, Copilot CLI, Copilot app).

Fleet Analytics is invite only: you need an API key.

## Install

Requires Python 3.9+ and a git repository.

**1. Install `faf`** (once per machine), then open a new terminal:

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/TheVanice/faf-collector/main/install.ps1 | iex
```

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/TheVanice/faf-collector/main/install.sh | sh
```

**2. Set up your project:**

```bash
cd <project>
faf install --init
```

Enter your API key when asked, then commit the files it lists. Teammates only run
`faf install`.

**3. Start a new agent session.** Claude Code: accept the workspace-trust dialog.

### Upgrade and remove

| Command | Does |
|---|---|
| `faf upgrade`, then `faf init` | update to the latest release |
| `faf uninit` | remove the hooks from this project |
| `faf uninstall` | remove the `faf` command |

## License

Source-available under the PolyForm Shield License 1.0.0 (see [LICENSE](LICENSE)).

## Contributing

This repository is published from a private development repository. Pull requests
are welcome and are ported there by hand.
