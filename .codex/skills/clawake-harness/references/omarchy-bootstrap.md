# Fresh checkout on Omarchy

1. Discover repositories and read their instructions; inspect Git status/remotes,
   branches/history and PRs without merging or rewriting history.
2. Inspect `uname -a`, `uname -m`, `/etc/os-release`, `omarchy version`, pacman
   version/repository headings and available yay/paru. Do not change channels,
   pacman.conf or perform a full system upgrade as a prerequisite shortcut.
3. Run the doctor. Inspect all user services/unit files, Quadlets under
   `~/.config/containers/systemd`, listening TCP/UDP ports, runtime directories
   and, when installed, `podman version`, `info`, `ps -a`, `network ls`, `images`
   and `volume ls`. Inventory names are not proof of ownership.
4. Resolve packages against configured repositories with `pacman -Ss
   '^(python|uv|podman|github-cli)$'`. Prefer repository packages and existing
   Omarchy conventions (`omarchy pkg add --help`), otherwise
   `sudo pacman -S --needed uv podman github-cli` with transaction review.
   Install Python only if the repository's required version is missing.
   Never add `--noconfirm`, use Debian tooling, or introduce another container
   runtime. Use an existing AUR helper only for genuinely unavailable packages.
5. Verify rootless Podman info under the operator UID. Check subuid/subgid only
   for mapping failures. Check XDG_RUNTIME_DIR and `loginctl show-user "$USER"`.
   Do not edit mapping files preemptively. User systemd must work; no daemon
   workaround. Inspect Linger; enable it only for a concrete logged-out runtime
   requirement after explaining that need.
6. Verify installed Quadlet generator/package files and
   `man podman-systemd.unit`; run repository generator tests where available.
   The user Quadlet root is `~/.config/containers/systemd`; Clawake generates
   production files. Generator existence alone is not proof it works.
7. From Clawake: `make install-dev`, export `CLAWAKE_PROJECT_ROOT="$PWD"`, then
   `uv run clawake --help`, `uv run pytest`, `uv run ruff check .` and the
   repository's CI checks, including `uv run ruff format --check .`.
8. Inspect and reuse ClawCAD's dedicated profile preparation on its verified
   source branch. Validate and preview through Clawake. Review two gateways,
   image pins, read-only mounts, network, known loopback ports and env references.
   Secure fresh tokens locally; never overwrite old env files. Gateway tokens
   are unique, A2A tokens are independent and match both ends of each direction.
   Use exclusive creation, private parents and mode 0600; never echo token values.
9. Deploy only after all execution gates in SKILL.md pass. Model access is
   configured AFTER setup through OpenClaw inside each container, using
   `uv run clawake onboard -c <inventory> -m <member> --execute`; see
   `docs/manual.md` section 2. Do not require model keys in env files.
   Keep the two existing named agents and recheck policies after onboarding. Check status/logs,
   actual capability policies and final same-task A2A response. Optional provider
   credentials remain NOT_CONFIGURED; missing model access blocks completion smoke.
10. Record actual evidence in the private coordinating GitHub issue when
    authenticated. READY means deployment and completion smoke passed, not just
    successful parsing or preview. Stop before engineering work.

The doctor is read-only and never installs, syncs dependencies, writes env files,
starts services or runs tests. It uses `uv run --no-sync --offline` only when a
local development environment already exists. Run tests explicitly and record
their separate evidence. Failed sandbox queries require a real-host retry;
permission errors are not evidence that runtime resources are absent.

## Podman 6 network-online wait on Omarchy

If setup waits on `podman-user-wait-network-online.service`, inspect its journal,
`systemctl is-active network-online.target` and the system wait-online service.
Omarchy may mask NetworkManager-wait-online and leave the target inactive while
network access already works. Do not unmask or enable global network services as
an incidental fix. Clawake supports `quadlet_default_dependencies: false` on
individual instances and networks; use this explicit inventory opt-out only
when the host evidence warrants it. Default true preserves existing inventories.
It renders `[Quadlet] DefaultDependencies=false` on container/network/volume
artifacts and omits explicit container network-online dependencies. This is NOT
systemd `[Unit] DefaultDependencies=false`. It removes startup ordering, not
network access; applications still need normal recovery from connectivity loss.
Regenerate and review the inventory, test with the installed generator and apply
through Clawake. A cancelled CLI client may leave queued systemd jobs; inspect
those and use the corrected Clawake lifecycle rather than editing installed units.
