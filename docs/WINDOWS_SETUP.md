# Glass on Windows

Use Windows PowerShell and a browser. You can try the demo without an AI account. For real work, use either Claude or Codex.

## 1. Get Glass and Python

1. Open the [Glass repository on GitHub](https://github.com/Ky13r13/glass-agents). Click **Code → Download ZIP**, then right-click the ZIP in File Explorer and choose **Extract All**. If someone sent you the Glass ZIP, start with **Extract All**.
2. Install the **Python Install Manager** from [Python for Windows](https://www.python.org/downloads/windows/).
3. Open the extracted folder that contains `README.md` and the `agentctl` folder. Click File Explorer’s address bar, type `powershell`, and press **Enter**. Paste each line below and press **Enter**.

```powershell
py install 3.13
py -3.13 -m agentctl dashboard --demo
```

If the browser doesn’t open, visit [the local dashboard](http://127.0.0.1:8765). Press **Ctrl+C** in PowerShell to stop the demo.

## 2. Connect the AI you have

In the same PowerShell window, run these two lines once.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[agents]"
```

**Claude Code only?** If Claude Code is already signed in and working in PowerShell, skip to step 3. Glass uses the Claude Agent SDK to draft and run Claude teams. You can use your working Claude Code session without creating a separate API key for Glass. If Glass reports a sign-in error, check that Claude Code works under the same Windows account that starts Glass.

**Using Codex?** Follow the [official Windows Codex installation steps](https://github.com/openai/codex#quickstart), reopen PowerShell in your Glass folder, then run `codex login` and follow the sign-in prompts. If Glass can’t find it, run `(Get-Command codex.exe).Source` and paste the displayed path into **Connections → Codex**.

## 3. Start your first project

These commands create and open a sample project folder under your Windows user folder. Keep PowerShell open while Glass is running.

```powershell
$project = Join-Path $env:USERPROFILE "Glass Projects\My First Project"
New-Item -ItemType Directory -Force -Path $project | Out-Null
.\.venv\Scripts\python.exe -m agentctl -C "$project" dashboard
```

Open **Teams** and describe your task. Under **Draft with**, choose **Claude Code** or **Codex**. Under **Runtimes the team can use**, check only the tools you have. If you only have Claude Code, check **Claude** and uncheck **Codex**. Click **Draft team**, review the proposal, then click **Save team** and **Launch team**. Choose **Start a new project** if the team should create the starting files.
