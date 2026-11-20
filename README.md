# claude-memguard

A small macOS watchdog that kills runaway processes started by AI desktop apps before they use up your Mac's memory. It guards Claude and ChatGPT by default, and you can add any other app.

Agents run shell commands unattended. When one of those commands blows up (a package manager bug, a script that loads a whole dataset, a leak), macOS has no per-process memory limit to stop it. The Mac starts swapping, everything freezes, and other work dies with it. `claude-memguard` kills just the runaway process. The agent sees exit code 137 and carries on.

It's one Python file using only the standard library, with no dependencies.

## How it works

Once a second it checks memory for every process the guarded apps launched: agent sessions, their shells, and anything those start, including background jobs whose parent has exited. macOS puts everything an app launches in the app's *resource coalition*, so membership is read from the coalition rather than from parent process IDs. The apps themselves are never killed.

| Rule | Trigger | Who gets killed |
| --- | --- | --- |
| 1 | One process over the per-process limit | That process |
| 2 | One app's processes together over the total limit | That app's largest process |
| 3 | System free memory at or below the free floor | The largest guarded process, if it is at least 1 GB |

Rules are checked in that order. Every kill is a `SIGKILL`. After a kill the guard waits 3 seconds for memory to be reclaimed before checking again.

Executables under `/System/Library/` are counted toward the total but never killed. These are macOS services an app runs inside its coalition, such as the virtual machine the Claude app uses.

## Default limits

Limits scale with physical RAM: **per-process 33%**, **total 55%**, **free floor 10%**. They are rounded to the nearest 0.5 GB.

| RAM | Per-process | Total per app |
| --- | --- | --- |
| 8 GB | 2.5 GB | 4.5 GB |
| 16 GB | 5.5 GB | 9 GB |
| 18 GB | 6 GB | 10 GB |
| 24 GB | 8 GB | 13 GB |
| 32 GB | 10.5 GB | 17.5 GB |
| 36 GB | 12 GB | 20 GB |
| 48 GB | 16 GB | 26.5 GB |
| 64 GB | 21 GB | 35 GB |
| 96 GB | 31.5 GB | 53 GB |
| 128 GB | 42 GB | 70.5 GB |

The ratios come from limits tuned by hand on a 36 GB Mac over a month of heavy agent use: 12 GB per process and 20 GB in total. That month it caught about 70 runaways, most of them a package manager bug that grew past 12 GB in under a minute.

- **33% per process:** no single command an agent runs should need a third of the machine. A runaway grows fast, so stopping it here leaves room before macOS starts swapping.
- **55% in total:** this leaves about 45% for macOS, the apps themselves, your browser and your editor.
- **10% free floor:** a last resort for when memory runs short system-wide.

## Install

```bash
git clone https://github.com/GabePZ/claude-memguard.git
cd claude-memguard
/usr/bin/python3 ./claude-memguard install
```

`install` does four things:

1. Copies the script to `~/.local/bin/claude-memguard`.
2. Writes a commented config to `~/.config/claude-memguard/config.ini`.
3. Adds a launchd agent that starts at login and restarts the guard if it dies.
4. Prints the limits in effect.

Any Python 3.9 or later works. The agent keeps using the Python you ran `install` with, and `/usr/bin/python3` is the one least likely to move.

## Configure

Edit `~/.config/claude-memguard/config.ini`. The running guard picks up changes within a second, with no restart needed.

```ini
[memguard]
per_process = 33%      # GB (12), a unit (12G, 512M), a share of RAM (33%), or off
total = 55%
min_free_pct = 10      # 0 turns the free-memory rule off
notify = true
apps =
    /Applications/Claude.app
    /Applications/ChatGPT.app
protect =
    /System/Library/
```

The same settings work as flags for one-off runs, and flags take precedence over the config file:

```bash
claude-memguard run --per-process 8G --total 50% --dry-run
```

`--dry-run` logs `WOULD KILL` instead of killing. Use it to try new limits.

## Check on it

```bash
claude-memguard status
```

`status` shows each limit and where it came from (default, config or flag). It also shows what each guarded app is using right now, with its largest processes. Kills are logged to `~/Library/Logs/claude-memguard.log`:

```bash
grep KILLED ~/Library/Logs/claude-memguard.log | tail
```

## When it fires

- **In the agent:** the command dies with exit code 137 (`Killed: 9`), and there is no other error.
- **On screen:** a macOS notification titled "Memory guard" names the process, its size and the rule it broke.
- **In the log:** one line per kill with the time, the PID, the size, the rule and the full command line.

## Caveats

- **Only processes the listed apps launch are guarded.** Claude Code or Codex running in a terminal belongs to the terminal app's coalition. Add your terminal (for example `/Applications/iTerm.app`) to `apps` to cover it, but then everything you launch from that terminal is guarded too.
- **The total rule kills the largest process, not the one that pushed memory over the limit.** A big job can die because other sessions are using memory.
- **Kills are immediate.** `SIGKILL` gives the process no chance to clean up or save partial output.
- **It checks once a second.** A process that grows faster than that can overshoot a limit before it is killed.

## Uninstall

```bash
claude-memguard uninstall
```

This removes the launchd agent and the installed script. It keeps your config and log.

## Development

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
