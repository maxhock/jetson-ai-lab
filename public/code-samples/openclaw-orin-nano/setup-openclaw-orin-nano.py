#!/usr/bin/env python3
"""
One-click setup: OpenClaw on Jetson Orin Nano (8GB)

Run:
    curl -fsSL https://raw.githubusercontent.com/.../setup-openclaw-orin-nano.py | python3

Or:
    python3 setup-openclaw-orin-nano.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from textwrap import fill
from urllib import error, request

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"
MODEL_ID = "google/gemma-4-E2B-it"
MODEL_NAME = "Gemma 4 E2B"


def format_cmd(cmd):
    if isinstance(cmd, str):
        return cmd
    return " ".join(cmd)


def run(cmd, check=True, capture=False, shell=None, timeout=None):
    if shell is None:
        shell = isinstance(cmd, str)

    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        fail(f"Command timed out: {format_cmd(cmd)}")

    if check and result.returncode != 0:
        print(f"{RED}Command failed: {format_cmd(cmd)}{RESET}")
        if capture:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            if stderr:
                print(stderr)
            elif stdout:
                print(stdout)
        sys.exit(1)
    return result


def run_output(cmd):
    return run(cmd, capture=True, check=False).stdout.strip()


def service_state(name):
    state = run_output(f"systemctl is-active {name} 2>/dev/null").strip()
    return state or "unknown"


def wait_for_service(name, timeout=30, interval=2):
    deadline = time.time() + timeout
    state = service_state(name)
    while time.time() < deadline:
        if state == "active":
            return state
        time.sleep(interval)
        state = service_state(name)
    return state


def recent_service_logs(name, lines=40):
    return run_output(f"sudo journalctl -u {name} --no-pager -n {lines} 2>&1")


def ensure_ollama_data_dir():
    run("sudo mkdir -p /usr/share/ollama")
    run("sudo chown -R ollama:ollama /usr/share/ollama")
    run("sudo chmod 755 /usr/share/ollama")


def step(number, title, summary=None):
    print(f"\n{BOLD}{CYAN}{'=' * 64}")
    print(f"  Step {number}: {title}")
    print(f"{'=' * 64}{RESET}")
    if summary:
        narrate(summary)
        print()


def narrate(message):
    paragraphs = [p.strip() for p in message.strip().split("\n\n") if p.strip()]
    for index, paragraph in enumerate(paragraphs):
        for line in fill(paragraph, width=78).splitlines():
            print(f"  {line}")
        if index != len(paragraphs) - 1:
            print()


def ok(message):
    print(f"  {GREEN}✓ {message}{RESET}")


def warn(message):
    print(f"  {YELLOW}⚠ {message}{RESET}")


def fail(message):
    print(f"  {RED}✗ {message}{RESET}")
    sys.exit(1)


def bullet(message):
    print(f"  - {message}")


def text_block(label, text):
    print(f"  {BOLD}{label}{RESET}")
    for line in fill(text.strip(), width=74).splitlines():
        print(f"    {line}")


def total_swap_bytes():
    output = run_output("swapon --noheadings --bytes --show=SIZE 2>/dev/null")
    total = 0
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            total += int(line)
        except ValueError:
            pass
    return total


def node_major(version):
    version = version.strip()
    if not version.startswith("v"):
        return 0
    try:
        return int(version[1:].split(".", 1)[0])
    except ValueError:
        return 0


def find_model_row(model_name):
    models = run_output("ollama list 2>&1")
    for line in models.splitlines():
        if line.strip().startswith(model_name):
            return line.strip()
    return ""


def ensure_swap(target_gb=16, path="/var/swapfile"):
    narrate(
        "Add swap headroom for installs and first model load. Target: 16 GB total swap."
    )

    target_bytes = target_gb * 1024 * 1024 * 1024
    current_swap = total_swap_bytes()
    current_swap_gb = current_swap / (1024 ** 3)

    if current_swap >= target_bytes:
        ok(f"Swap already active ({current_swap_gb:.1f} GB)")
        return

    free_bytes = shutil.disk_usage("/").free
    reserve_bytes = 4 * 1024 * 1024 * 1024
    if free_bytes < target_bytes + reserve_bytes:
        warn(
            f"Only {free_bytes / (1024 ** 3):.1f} GB free on /. "
            f"Skipping auto swap setup because {target_gb} GB may be too tight."
        )
        warn(
            "Free some disk space and create swap manually if installs start swapping "
            "heavily or getting killed."
        )
        return

    print(f"  Current active swap: {current_swap_gb:.1f} GB")
    print(f"  Creating or resizing {path} to {target_gb} GB...")

    is_active = run(
        f"sudo swapon --show=NAME | awk '$1 == \"{path}\" {{ found=1 }} END {{ exit(found ? 0 : 1) }}'",
        check=False,
    ).returncode == 0
    if is_active:
        run(f"sudo swapoff {path}")

    run(f"sudo fallocate -l {target_gb}G {path}")
    run(f"sudo chmod 600 {path}")
    run(f"sudo mkswap {path}")
    run(f"sudo swapon {path}")

    fstab_line = f"{path} none swap sw 0 0"
    current_fstab = run_output("sudo cat /etc/fstab 2>/dev/null")
    if fstab_line not in current_fstab.splitlines():
        run(f"echo '{fstab_line}' | sudo tee -a /etc/fstab >/dev/null")

    final_swap = total_swap_bytes()
    ok(f"Swap ready ({final_swap / (1024 ** 3):.1f} GB active)")


def verify_tool_calling():
    narrate(
        "Verify native tool calling in Ollama before starting OpenClaw."
    )

    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": "What is the weather in Madrid?"}],
        "stream": False,
        "options": {"num_ctx": 16384},
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for a city",
                    "parameters": {
                        "type": "object",
                        "required": ["city"],
                        "properties": {
                            "city": {
                                "type": "string",
                                "description": "City name",
                            }
                        },
                    },
                },
            }
        ],
    }

    try:
        req = request.Request(
            "http://127.0.0.1:11434/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        warn(f"Tool-calling check failed: {exc}")
        return

    tool_calls = data.get("message", {}).get("tool_calls") or data.get("tool_calls") or []
    if not tool_calls:
        warn("The model replied without a structured tool call. OpenClaw may still work, but this is worth retesting.")
        return

    first_call = tool_calls[0]
    function = first_call.get("function", {})
    arguments = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            pass

    ok("Structured tool calling is working")
    print(f"  Tool name: {function.get('name', 'unknown')}")
    print(f"  Tool arguments: {json.dumps(arguments, ensure_ascii=True)}")


def talk_to_agent():
    user_message = (
        "Hello OpenClaw. Introduce yourself in 3 short sentences. Mention that you "
        f"are running locally on a Jetson Orin Nano with {MODEL_NAME}, and mention one "
        "thing you can do."
    )
    text_block("Prompt:", user_message)

    response = run(
        [
            "openclaw",
            "agent",
            "--to",
            "+0000000000",
            "--message",
            user_message,
            "--thinking",
            "off",
            "--json",
        ],
        capture=True,
        timeout=300,
    )

    try:
        data = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        fail(f"Could not parse OpenClaw response as JSON: {exc}")

    result = data.get("result", {})
    payloads = result.get("payloads", [])
    meta = result.get("meta", {})
    agent_meta = meta.get("agentMeta", {})
    usage = agent_meta.get("usage", {})

    reply = ""
    for payload in payloads:
        if payload.get("text"):
            reply = payload["text"].strip()
            break

    if not reply:
        warn("The gateway answered, but the first agent response was empty.")
        return

    ok("Agent responded")
    text_block("Reply:", reply)

    duration_ms = meta.get("durationMs", 0)
    print(
        f"  Tokens: in={usage.get('input', '?')} out={usage.get('output', '?')} "
        f"total={usage.get('total', '?')} | duration={duration_ms}ms"
    )


def main():
    print(
        f"""
{BOLD}{CYAN}╔════════════════════════════════════════════════════════════════╗
║      OpenClaw on Jetson Orin Nano — guided setup log         ║
╚════════════════════════════════════════════════════════════════╝{RESET}
"""
    )

    narrate(
        f"Install Ollama, pull {MODEL_NAME}, install OpenClaw, write config, start the gateway, and send one test message."
    )
    print()

    if "aarch64" not in run_output("uname -m"):
        fail("This script is meant for ARM64 Jetson devices. Exiting.")

    if os.geteuid() == 0:
        fail("Do not run this script as root. Run it as your regular user and let it call sudo when needed.")

    step(
        1,
        "Prepare memory headroom",
        "Prepare swap so the 8 GB Jetson has enough memory headroom.",
    )
    ensure_swap(target_gb=16)

    step(
        2,
        "Install and tune Ollama",
        "Install the local inference runtime and apply 8 GB memory tuning.",
    )

    ollama_installed = bool(shutil.which("ollama"))
    if ollama_installed:
        ok(f"Ollama already installed ({run_output('ollama --version')}). Skipping install.")
    else:
        narrate(
            "Install Ollama for local inference on JetPack 6 ARM64."
        )
        run("curl -fsSL https://ollama.com/install.sh | sh")
        ok("Ollama installed")

    env_conf = "/etc/systemd/system/ollama.service.d/environment.conf"
    target_env = (
        '[Service]\n'
        'Environment="OLLAMA_FLASH_ATTENTION=1"\n'
        'Environment="OLLAMA_KV_CACHE_TYPE=q8_0"\n'
        'Environment="OLLAMA_KEEP_ALIVE=1h"\n'
    )

    current_env = run_output(f"cat {env_conf} 2>/dev/null")
    ollama_env_ready = all(
        item in current_env
        for item in (
            'Environment="OLLAMA_FLASH_ATTENTION=1"',
            'Environment="OLLAMA_KV_CACHE_TYPE=q8_0"',
            'Environment="OLLAMA_KEEP_ALIVE=1h"',
        )
    )
    if ollama_env_ready:
        ok("Ollama tuning already configured. Skipping override update.")
    else:
        narrate(
            "Write a small Ollama override for lower memory use and faster warm starts."
        )
        bullet("OLLAMA_FLASH_ATTENTION=1")
        bullet("OLLAMA_KV_CACHE_TYPE=q8_0")
        bullet("OLLAMA_KEEP_ALIVE=1h")
        run("sudo mkdir -p /etc/systemd/system/ollama.service.d")
        fd, tmp = tempfile.mkstemp(suffix=".conf")
        with os.fdopen(fd, "w") as handle:
            handle.write(target_env)
        run(f"sudo cp {tmp} {env_conf}")
        os.unlink(tmp)
        run("sudo systemctl daemon-reload")
        run("sudo systemctl restart ollama")
        ok("Ollama override written and service restarted")

    ensure_ollama_data_dir()

    ollama_state = service_state("ollama")
    if ollama_state == "active":
        ok("Ollama service already active")
    else:
        print(f"  Starting Ollama service ({ollama_state})...")
        run("sudo systemctl start ollama")
        ollama_state = wait_for_service("ollama", timeout=30, interval=2)

    if ollama_state != "active":
        logs = recent_service_logs("ollama", lines=30)
        fail(f"Ollama service is not running ({ollama_state}).\n{logs}")
    ok("Ollama service is active")

    step(
        3,
        f"Download {MODEL_NAME}",
        f"Pull {MODEL_NAME}, a compact edge-focused model with solid tool calling.",
    )

    models = run_output("ollama list 2>&1")
    if MODEL_ID in models:
        ok(f"{MODEL_ID} already downloaded. Skipping pull.")
    else:
        narrate(
            "Download size is about 5 GB. Gemma 4 E2B is designed for 8 GB-class edge devices, so keep swap enabled for installs and the first warm load."
        )
        run(f"ollama pull {MODEL_ID}")
        ok("Model downloaded")

    model_row = find_model_row(MODEL_ID)
    if model_row:
        print(f"  Ollama inventory: {model_row}")

    verify_tool_calling()

    step(
        4,
        "Install Node.js 22 and OpenClaw",
        "Install the runtime OpenClaw needs, then install the OpenClaw CLI and gateway.",
    )

    node_ver = run_output("node --version 2>/dev/null")
    if node_major(node_ver) >= 22:
        ok(f"Node.js {node_ver} already installed")
    else:
        narrate(
            "Install Node.js 22 from NodeSource."
        )
        run("curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -")
        run("sudo apt install -y nodejs")
        ok(f"Node.js {run_output('node --version')} installed")

    if shutil.which("openclaw"):
        ok(f"OpenClaw already installed ({run_output('openclaw --version')})")
    else:
        narrate(
            "Install the latest OpenClaw globally with npm."
        )
        run("sudo npm install -g openclaw@latest")
        ok(f"OpenClaw {run_output('openclaw --version')} installed")

    step(
        5,
        "Write a low-memory OpenClaw config",
        "Write a local config with a 16K context window and a minimal tool profile.",
    )

    home = os.path.expanduser("~")
    oc_dir = os.path.join(home, ".openclaw")
    ws_dir = os.path.join(oc_dir, "workspace")
    config_path = os.path.join(oc_dir, "openclaw.json")

    os.makedirs(ws_dir, exist_ok=True)

    config = {
        "models": {
            "providers": {
                "ollama": {
                    "baseUrl": "http://127.0.0.1:11434",
                    "apiKey": "ollama-local",
                    "api": "ollama",
                    "models": [
                        {
                            "id": MODEL_ID,
                            "name": MODEL_NAME,
                            "contextWindow": 16384,
                        }
                    ],
                }
            }
        },
        "tools": {"profile": "minimal"},
        "gateway": {
            "port": 19000,
            "mode": "local",
            "auth": {"mode": "token", "token": "my-jetson-nano-token"},
        },
    }

    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    ok("Config written")

    bullet("contextWindow = 16384 keeps the model usable without overcommitting memory")
    bullet("tools.profile = minimal reduces the attack surface and prompt overhead")
    bullet("gateway.mode = local keeps the endpoint bound to localhost")

    run(["openclaw", "models", "set", f"ollama/{MODEL_ID}"])
    ok("Default model set")

    workspace_files = {
        "AGENTS.md": "# Personal assistant",
        "SOUL.md": "Be concise and helpful.",
        "TOOLS.md": "Use tools only when needed.",
        "USER.md": "Name: Your Name",
        "IDENTITY.md": "OpenClaw on Jetson Orin Nano",
        "HEARTBEAT.md": "",
        "BOOTSTRAP.md": "",
    }
    for name, content in workspace_files.items():
        with open(os.path.join(ws_dir, name), "w", encoding="utf-8") as handle:
            handle.write(content + "\n")
    ok("Workspace files created")

    validate = run_output("openclaw config validate 2>&1")
    if "Config valid" in validate:
        ok("Config valid")
    else:
        fail(f"Config validation failed: {validate}")

    run("sudo loginctl enable-linger $USER", check=False)
    ok("Linger enabled for headless and SSH use")

    step(
        6,
        "Launch the gateway and send a real message",
        "Start the local gateway and validate the full OpenClaw to Ollama path.",
    )

    run("systemctl --user stop openclaw-gateway 2>/dev/null", check=False)
    run("systemctl --user reset-failed openclaw-gateway 2>/dev/null", check=False)
    run("systemd-run --user --unit=openclaw-gateway openclaw gateway run")
    print("  Waiting for the gateway to come up...")
    time.sleep(8)

    probe = run_output("openclaw channels status --probe 2>&1")
    if "Gateway reachable" in probe:
        ok("Gateway is up")
    else:
        fail(f"Gateway not reachable: {probe}")

    narrate(
        "Send one short prompt and print the reply."
    )
    print()
    talk_to_agent()

    ps = run_output("ollama ps 2>&1")

    print(
        f"""
{BOLD}{GREEN}╔════════════════════════════════════════════════════════════════╗
║                         Setup complete!                      ║
╚════════════════════════════════════════════════════════════════╝{RESET}

{BOLD}Loaded model status:{RESET}
{ps}

{BOLD}Talk to your agent:{RESET}
  openclaw agent --to +0000000000 --message "your message" --thinking off

{BOLD}Gateway management:{RESET}
  systemctl --user restart openclaw-gateway
  systemctl --user stop openclaw-gateway
  openclaw channels status --probe

{BOLD}Optional video demos from this tutorial repo:{RESET}
  python3 endurance-test.py
  python3 multi-agent-debate.py --demo

{BOLD}Optional — add WhatsApp:{RESET}
  openclaw channels login --channel whatsapp
"""
    )


if __name__ == "__main__":
    main()
