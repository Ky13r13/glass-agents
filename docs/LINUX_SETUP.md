# Linux / 16 GB RAM / GTX 1080 Ti

## Division of work

Use hosted Codex for code changes, architecture decisions, and difficult debugging. Use a local 7B quantized model for focused source explanations and small analysis tasks. Hosted inference does not use your GPU; compilers, tests, and local models still use your machine's resources.

The starting configuration is:

| Setting | Starting value | Reason |
|---|---|---|
| Base model | `qwen2.5-coder:7b-instruct-q4_K_M` | Modest download and a focused coding model to benchmark. |
| Local context | 8192 tokens | Leaves more memory headroom than a very large context. |
| Local packet estimate | 2500 tokens | Leaves room for formatting and generated output. |
| Local output allowance | 1024 tokens | Bounded analysis, not long autonomous work. |
| Concurrent local requests | 1 | Controls KV-cache and host memory pressure. |
| Loaded local models | 1 | Avoids competing model allocations. |
| Hosted readers | At most 2 | Limits duplicated work and local process overhead. |
| Writing tasks | 1, exclusive | Prevents concurrent edits in the shared working directory. |

These values are starting choices, not measured performance claims. Actual memory use depends on the model, runtime version, context, display use, and other processes.

## 1. Check existing prerequisites

```bash
python3 --version
git --version
codex --version
codex login status
nvidia-smi
```

Python must be 3.11 or newer. Use your distribution's package manager if necessary. If you already use Codex, keep that installation and sign-in. If it is missing, follow the official [Codex CLI documentation](https://learn.chatgpt.com/docs/codex-cli).

Ollama's hardware documentation lists the GTX 1080 Ti under compute capability 6.1. As checked on 2026-10-01, it specifies driver version 570 or newer for compute capability 5.0 through 6.2. Select a proprietary driver branch supported by both your Linux distribution and the Pascal GPU; do not assume every newer driver branch still supports every older card. agentctl does not change GPU drivers.

If `nvidia-smi` fails, resolve that before assuming GPU acceleration is working. `agentctl doctor` prints the observed card, free VRAM, driver, available RAM, and runtime information; it cannot install hardware drivers.

## 2. Install agentctl

```bash
cd /path/to/agentctl-v0.1.0
bash install.sh
export PATH="$HOME/.local/bin:$PATH"
agentctl doctor
```

Put the PATH export in your shell's startup file if needed. No third-party Python packages are required. The installation is a single executable zipapp and works from any project directory.

## 3. Install or reuse Ollama

Follow the official [Ollama Linux installation guide](https://docs.ollama.com/linux). Its installer can set up a system service, so check for an existing service before starting another server:

```bash
ollama --version
systemctl status ollama
```

If a service is already running, configure that service rather than starting a competing listener. The following override sets the intended resource profile:

```bash
sudo systemctl edit ollama
```

Add:

```ini
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_CONTEXT_LENGTH=8192"
Environment="OLLAMA_NO_CLOUD=1"
```

Then apply it:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

If you prefer a foreground server and no service is listening, run `agentctl models serve`. It applies these settings to its own child process and stays in the terminal. Ctrl-C stops that server. It does not alter an existing service or install anything privileged. Avoid forcing Flash Attention on this older GPU; start with runtime defaults.

Changing `.agentctl/config.toml` cannot change the environment of an already-running Ollama service. The direct analysis API supplies `num_ctx` on each request, but service concurrency must be configured on the server.

## 4. Download, measure, and inspect placement

```bash
agentctl models setup
agentctl models benchmark
agentctl models status
ollama ps
```

The default base model is about 4.7 GB on disk; reserve additional space and memory for runtime overhead. `models setup` creates the alias `agentctl-helper:latest`. It does not train or fine-tune a model.

Inspect model placement with `ollama ps` and the reported VRAM allocation. CPU offloading may still work but can change latency substantially. Do not choose a larger model just because its file fits in nominal VRAM: context memory and overhead also count.

The benchmark checks whether one small Python-reading question is answered as correct structured JSON and reports observed generation tokens per second. Use it to confirm the installation, then try representative project tasks. A tiny smoke test is not evidence of reliable autonomous coding.

If memory is tight, reduce `local_num_ctx` to 4096 and `local_context_budget` to 1500 in `.agentctl/config.toml`, then rerun `models setup` for the alias. If you need a smaller model, use a compatible explicit `--base` with `models setup`. The app never downloads a replacement model without a setup command.

## 5. Use the local helper on a project

```bash
cd /path/to/project
agentctl init
agentctl start "Explain the CSV export behavior and edge cases" --backend local
```

Only the prepared packet is sent by direct local analysis. The worker cannot browse the rest of the repository or run tools. For changes, start a hosted Codex task, or use a reviewed team plan with local readers and a hosted writer.

## Troubleshooting

| Symptom | Check |
|---|---|
| `agentctl: command not found` | Add `~/.local/bin` to PATH or call the absolute executable path. |
| `No module named tomllib` | Use Python 3.11+. |
| Ollama connection refused | Start the service or a foreground server, then retry. |
| Model not found | Run `agentctl models setup` against the same Ollama server. |
| GPU absent or CPU inference | Check NVIDIA driver support, free VRAM, and `ollama ps`. |
| Packet exceeds budget | Narrow the task/includes or shorten overly large instruction/handoff files. |
| Codex process fails | Read that run's `stderr.log`; check `codex --version` and login status. |
| Permission denied in a scripted task | Use interactive Codex to handle the needed approval; the wrapper does not bypass it. |
| `running` after a crash/power loss | No automatic replay occurs. Inspect artifacts and use `checkpoint` / `resume` to start fresh. |

## Sources checked 2026-10-01

- [Ollama hardware support](https://docs.ollama.com/gpu)
- [Ollama Linux setup](https://docs.ollama.com/linux)
- [Ollama context length](https://docs.ollama.com/context-length)
- [Ollama concurrency and server settings](https://docs.ollama.com/faq)
- [Qwen2.5 Coder 7B Q4_K_M model entry](https://ollama.com/library/qwen2.5-coder:7b-instruct-q4_K_M)
- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)

Runtime requirements and model tags can change. `doctor` reports what is actually installed; this package includes no GPU binary or model weights.
