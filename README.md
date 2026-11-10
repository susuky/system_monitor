# Vital

**English** | [繁體中文](README.zh-TW.md)

A Linux terminal system monitor for CPU, NVIDIA GPU, memory, processes, disks, and network traffic. Vital brings the information you would normally check with `free -h`, `htop`, `nvtop`, `df -h`, `lscpu`, and `mpstat` into one view.

It refreshes every second by default, supports English and Traditional Chinese, and can export text reports or JSON snapshots.

## Interface

![Vital overview in English](docs/images/overview-en.png)

The overview shows per-core CPU load, memory, GPUs, processes, and disk capacity together. These screenshots were captured from the application using sample data.

![Vital GPU processes in English](docs/images/gpu-en.png)

<details>
<summary>Traditional Chinese interface</summary>

![Vital overview in Traditional Chinese](docs/images/overview-zh-TW.png)

![Vital GPU processes in Traditional Chinese](docs/images/gpu-zh-TW.png)

</details>

| Tab | Contents |
| --- | --- |
| Overview | CPU, memory, GPU, and network summaries; per-core load; process search; disk capacity |
| CPU | Usage, user, sys, iowait, and idle time for each logical core, plus core, socket, and NUMA topology |
| Processes | Sort by CPU or memory; search by name, full command, PID, or user |
| GPU | Usage, VRAM, temperature, power, and fan speed for each NVIDIA GPU; GPU%, CPU%, VRAM, and RAM for compute and graphics processes |
| Disks / Network | Filesystem capacity, disk read/write rates and IOPS, and traffic for each network interface |
| Hardware | CPU model, caches, OS version, and memory, cache, and swap details |

Wide terminals display core load in multiple columns. Narrow terminals use a scrollable vertical layout. The top summaries are hidden below 32 rows; memory details remain available in the Hardware tab.

## Getting Started

### Requirements

- Linux (x86_64 or aarch64) or WSL2, with Python 3.10 or later, `venv`, and `pip`.
- A terminal with Unicode support. The Traditional Chinese interface also needs a Chinese font.
- Discrete NVIDIA GPUs are read through the driver's NVML library. Jetson Orin (JetPack 5) uses GPU load and temperature readings exposed by the system.

On WSL2, install and run Vital inside your Linux distribution. CPU, memory, and process values describe the WSL environment. NVIDIA GPU data comes from NVML provided by the Windows driver. The GPU process list may be incomplete, and per-process GPU usage is shown as `—` to avoid mixing Windows and Linux PIDs. Per-process VRAM is also shown as `—` when the driver does not provide it, or `null` in JSON. See [NVIDIA's WSL support limitations](https://docs.nvidia.com/cuda/wsl-user-guide/index.html#features-not-yet-supported).

Jetson Orin's CPU and GPU share system RAM, so Vital displays `Shared system RAM`. The current sysfs source does not provide per-process GPU data, dedicated VRAM usage, or separate GPU power readings. Missing values are `null` in JSON.

### Install and Run

```bash
git clone https://github.com/susuky/system_monitor.git
cd system_monitor
python3 -m venv .venv
.venv/bin/python -m pip install -e .
source .venv/bin/activate
vital --lang en
```

To run from the project directory without activating the virtual environment:

```bash
./vital.sh
```

If you use `uv`, replace the environment creation and installation steps with:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e .
```

### Run `vital` from Any Directory

From the project directory, create a command link for your account:

```bash
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/.venv/bin/vital" "$HOME/.local/bin/vital"
export PATH="$HOME/.local/bin:$PATH"
```

Add the last line to your shell's startup file, such as `~/.bashrc` for Bash, to make `vital` available in new terminals. Normal monitoring does not require root privileges.

### Language

```bash
vital --lang en
vital --lang zh-TW
```

The default interface language is Traditional Chinese. You can also press `l` in the interface to switch languages. Both methods save your choice in `~/.config/vital/config.json`, or `$XDG_CONFIG_HOME/vital/config.json`. Command help and text reports use the selected language as well. When upgrading from an older version, Vital uses the language choice in `~/.config/sysmon/config.json`. The legacy `sysmon` command and `./sysmon.sh` launcher remain available.

## Controls

| Key | Action |
| --- | --- |
| `1`–`6` | Switch tabs |
| `Space` | Pause or resume display updates |
| `/` | Search processes |
| `Esc` | Clear the search and return to the table |
| `c` / `m` | Sort processes by CPU / memory |
| `b` | Sort cores by ID or usage |
| `l` | Switch between English and Traditional Chinese and save the choice |
| Arrow keys | Move the table selection |
| `Ctrl+C` / `q` | Quit; `Ctrl+C` also quits while the search field is focused |

Use the `>1%` checkbox in Overview or CPU to show only busy cores. Overview lists the top 100 processes. The Processes tab lists all matching processes and shows the full command for the selected row. The GPU tab lists compute and graphics processes; use the arrow keys to select a process and view its full command. Tables can scroll horizontally in narrow terminals.

## Sampling and Export

```bash
vital --interval 0.5
vital --once
vital --interval 1 --samples 5
vital --json
vital --json -i 1 -n 5 > samples.ndjson
vital --help
```

`--once` prints one resource summary. `--samples` includes per-core user, system, iowait, idle, and busy percentages, similar to `mpstat 1 5`. Vital waits one sampling interval at startup to calculate CPU usage and transfer rates. The minimum interval is 0.2 seconds.

`--json` exports a complete snapshot. With `--samples`, it writes one JSON object per line. JSON field names are independent of the interface language. Snapshots include hostnames, process commands, users, and network addresses; remove sensitive information before sharing them.

## Metrics and Sensors

- Sizes use binary units: KiB, MiB, and GiB. Used memory is total memory minus available memory.
- Cache includes reclaimable kernel memory (`SReclaimable`); buffers are listed separately.
- Process CPU usage treats one logical core as 100%, so multithreaded processes can exceed 100%. Overall and per-core busy percentages exclude idle and iowait.
- CPU temperature comes from hardware sensors. CPU power is calculated from changes in Linux RAPL package energy counters.
- Per-process GPU% comes from recent NVML SM utilization samples. It is shown as `—` when no recent sample is available or the hardware does not support it. VRAM percentage is the process's share of the GPU's total VRAM; process RAM is RSS.
- Unavailable sensors and permission-restricted readings appear as `—`, or `null` in JSON. The CPU tab shows the status of power readings.

## Profiling

These measurements show Vital's own resource usage, with one logical core equal to 100% CPU. All platforms used a real PTY, a one-second refresh interval, and a 160 × 44 terminal. Each tab had a five-second warmup, followed by 30 seconds of measurement on Linux PC and Orin, or 20 seconds on WSL2.

| Platform | Processes | Overview CPU | Processes tab CPU | RSS |
| --- | ---: | ---: | ---: | ---: |
| Linux PC, 48 threads | 1,249 | 7.00% | 6.87% | 73.1–79.8 MiB |
| Orin NX, 8 cores | 311 | 6.03% | 5.47% | 49.1–50.6 MiB |
| Orin Nano, 6 cores | 300 | 9.77% | 8.60% | 48.7–50.3 MiB |
| WSL2 PC, 32 threads | 67 | 1.95% | 1.45% | 76.3–76.7 MiB |

Overhead varies with process count, core count, GPU driver, terminal size, and hardware speed. To measure it on your system:

```bash
.venv/bin/python scripts/benchmark.py
```

The script measures each tab for 30 seconds by default; use `--seconds 20` to change the duration. Its output includes collector CPU time, process count, TUI CPU usage, and RSS.

To locate CPU bottlenecks, profile the UI and collector threads separately in a real PTY:

```bash
.venv/bin/python scripts/profile_runtime.py --output artifacts/profile-ui --profile ui
.venv/bin/python scripts/profile_runtime.py --output artifacts/profile-collector --profile collector
```

The table above uses `benchmark.py` without a profiler. Profiling adds overhead and is intended for locating bottlenecks. On Python 3.12, the profiler may include other threads; the script also measures each component with a thread CPU clock.

## Copyright

Third-party dependencies retain their respective licenses.

```text
Copyright © 2026 Hao-Ping Lin (@susuky). All rights reserved.

This repository is publicly accessible for viewing and evaluation only.
No license is granted to use, copy, modify, redistribute, sublicense,
or use this software commercially without prior written permission
from the copyright holder.
```

See [LICENSE](LICENSE) for the full notice.
