# bf4_updater

Small Python CLI with two modes, selected per invocation by `--mode`:

1. **`--mode fwpkg`** — download a BlueField-4 PLDM `.fwpkg` from an
   Artifactory directory URL and push it to the BF4 **BMC** via Redfish
   `UpdateService`, then wait for the firmware update task to finish.
2. **`--mode os`** — download a DOCA OS `.iso` from a direct URL, serve
   it over HTTP from a lab bench, and verify the published URL is
   reachable. The actual OS install on the BF4 is then performed
   manually (e.g. via the BMC's virtual-media UI pointed at the
   printed URL).

Designed to run on a dedicated lab runner that has network access both
to Artifactory / the ISO mirror and to the BF4 BMC(s).

## Layout

```
bf4_updater/
  update_bf4.py          # CLI entry point
  config.py              # benches.yaml loader / validator
  artifactory.py         # discover *.fwpkg in an Artifactory dir
  downloader.py          # resumable HTTP download w/ progress + sha256
  redfish_fw.py          # multipart .fwpkg push to BMC + TaskService poll
  iso_server.py          # local HTTP server for the ISO
  benches.example.yaml   # template (committed)
  benches.yaml           # real bench creds (gitignored, you create it)
  requirements.txt
  downloads/             # auto-created cache for fwpkg + iso (gitignored)
```

## Install

Python 3.9+ recommended.

```bash
cd bf4_updater
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure benches

The example ships with two flavours of entry, all pre-filled with lab
credentials. Copy it to the gitignored real config:

```bash
cp benches.example.yaml benches.yaml
```

### BF4 benches (BMC + host)

Use as `--bf4` (firmware target) and/or `--http_server_bench`. Lab
BMC creds: `admin` / `Nvidia_12345!`.

| Bench name              | BF4 host (DOCA)                                                  | BMC                                                                   |
| ----------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------- |
| `sw-mtx-perf-003-bf4`   | `sw-mtx-perf-003-bf4.mtx.nbulabs.nvidia.com` (`10.9.156.23`)     | `sw-mtx-perf-003-bf4-bmc.mtx.nbulabs.nvidia.com` (`10.9.156.24`)      |
| `sw-mtx-062-bf4`        | `sw-mtx-062-bf4.mtx.nbulabs.nvidia.com` (`10.9.153.44`)          | `sw-mtx-062-bf4-bmc.mtx.nbulabs.nvidia.com` (`10.9.153.24`)           |
| `sw-mtx-065-bf4`        | `sw-mtx-065-bf4.mtx.nbulabs.nvidia.com` (`10.9.153.45`)          | `sw-mtx-065-bf4-bmc.mtx.nbulabs.nvidia.com` (`10.9.153.46`)           |

### Host-only benches (no BF4 attached)

Plain x86 lab hosts you can use as `--http_server_bench` to host the
ISO from while updating a BF4 elsewhere. Lab SSH creds: `root` /
`3tango` (recorded in the YAML for reference; the script doesn't SSH
into them today).

| Bench name      | Host                                                       |
| --------------- | ---------------------------------------------------------- |
| `sw-mtx-perf-003` | `sw-mtx-perf-003.mtx.nbulabs.nvidia.com` (`10.9.156.31`) |
| `sw-mtx-062`    | `sw-mtx-062.mtx.nbulabs.nvidia.com` (`10.9.153.27`)        |
| `sw-mtx-063`    | `sw-mtx-063.mtx.nbulabs.nvidia.com` (`10.9.153.207`)       |
| `sw-mtx-065`    | `sw-mtx-065.mtx.nbulabs.nvidia.com` (`10.9.153.211`)       |
| `sw-mtx-047`    | `sw-mtx-047.mtx.nbulabs.nvidia.com` (`10.9.151.104`)       |
| `sw-mtx-048`    | `sw-mtx-048.mtx.nbulabs.nvidia.com` (`10.9.151.105`)       |

Host-only entries are rejected if you try to use them as `--bf4`
(they have no BMC info).

### Schema (per bench)

Required:

- `host` — DNS name (preferred) or IP of the machine.

BF4-only required (only when used as `--bf4`):

- `bmc_host`, `bmc_user`, `bmc_pass`.

Optional:

- `host_fqdn`, `bmc_fqdn` — alternate FQDN; takes precedence over
  `host` / `bmc_host` when present.
- `host_user`, `host_pass` — SSH login info for the host
  (informational; not used by the script today).

## CLI shape

Each invocation runs exactly one mode, selected by `--mode`:

| Mode             | Required flags                              | What it does                                                                 |
| ---------------- | ------------------------------------------- | ---------------------------------------------------------------------------- |
| `--mode fwpkg`   | `--bf4 <bench>` + `--pldm-url <url>`        | Push `.fwpkg` from Artifactory to the BF4's BMC via Redfish `UpdateService`. |
| `--mode os`      | `--http_server_bench <bench>` + `--iso-url` | Download the DOCA OS `.iso` and serve it over HTTP from the named bench.     |

`--bf4 <bench>` names the bench whose **BMC** receives the firmware
update. `--http_server_bench <bench>` names the bench whose **host**
runs the local HTTP server (its `host` / `host_fqdn` is the URL the
BMC will reach for the ISO). The two roles can point at the same bench
across separate `--mode fwpkg` and `--mode os` runs, or at different
benches (e.g. serve the ISO from bench A, update bench B's firmware).

## Usage

```bash
python update_bf4.py --help
```

### Firmware update (`--mode fwpkg`)

`urm.nvidia.com` does **not** allow anonymous access, so you must supply
an Artifactory credential or the directory listing fails with `403`.
Generate an **identity token** at <https://urm.nvidia.com> (log in with
SSO → click your name → *Edit Profile* / *Generate an Identity Token*),
then either export it or pass it on the command line:

```bash
export ARTIFACTORY_TOKEN=<your-identity-token>   # sent as a Bearer token
```

Other accepted env vars: `ARTIFACTORY_API_KEY` (sent as `X-JFrog-Art-Api`)
or `ARTIFACTORY_USER` + `ARTIFACTORY_PASSWORD` (HTTP basic; the password
may be an identity token / API key).

```bash
python update_bf4.py \
  --mode fwpkg \
  --bf4      sw-mtx-perf-003-bf4 \
  --pldm-url https://urm.nvidia.com/artifactory/sw-mlnx-bluefield-generic/bluefield4/PLDM/20260510/Dev/MT_0000001775-01/
```

Or pass the token inline instead of exporting it:

```bash
python update_bf4.py \
  --mode fwpkg \
  --artifactory-token <your-identity-token> \
  --bf4      sw-mtx-perf-003-bf4 \
  --pldm-url https://urm.nvidia.com/artifactory/sw-mlnx-bluefield-generic/bluefield4/PLDM/20260510/Dev/MT_0000001775-01/
```

### OS ISO staging (`--mode os`) — serve from the same bench

```bash
python update_bf4.py \
  --mode os \
  --http_server_bench sw-mtx-perf-003-bf4 \
  --iso-url https://nbu-nfs.gtm.nvidia.com/auto/sw_mc_soc_release/doca_dpu/doca_3.3_bf4/20260511/ISO/bf4-os-doca-bundle-3.3.0-335_26.01_ubuntu-24.04_64k.iso
```

### OS ISO staging (`--mode os`) — serve from a different bench

```bash
python update_bf4.py \
  --mode os \
  --http_server_bench sw-mtx-047 \
  --iso-url https://nbu-nfs.gtm.nvidia.com/auto/sw_mc_soc_release/doca_dpu/doca_3.3_bf4/20260511/ISO/bf4-os-doca-bundle-3.3.0-335_26.01_ubuntu-24.04_64k.iso
```

The script downloads the ISO into `downloads/iso/`, starts an HTTP
server on `0.0.0.0:8000` (override with `--http-port` / `--http-bind`),
verifies the URL responds with the right size, and then prints a
banner like:

```
========================================
  ISO ready on bench: sw-mtx-perf-003-bf4
  File   : bf4-os-doca-bundle-3.3.0-335_26.01_ubuntu-24.04_64k.iso
  URL    : http://10.9.156.23:8000/bf4-os-doca-bundle-3.3.0-335_26.01_ubuntu-24.04_64k.iso
  Verify : curl -I http://10.9.156.23:8000/bf4-os-doca-bundle-...
========================================
```

The advertised host in the URL comes from the `--http_server_bench`'s
`host` (or `host_fqdn` if set). Override with `--http-advertise` if the
URL needs to be reached via a different name/IP.

Use the URL in the BMC virtual-media UI to install the OS on the BF4.
Press `Ctrl+C` when done to stop the HTTP server.

## What the firmware step does

1. List the Artifactory directory via the JSON storage API
   (`/artifactory/api/storage/<repo>/<path>?list&deep=0`).
2. Pick the **single** `*.fwpkg`. If there are zero or more than one,
   the script aborts (re-run with a more specific `--pldm-url`).
3. Download the package into `downloads/firmware/` (resumable,
   checksum-verified if the Artifactory metadata exposes a sha256).
4. POST it as a multipart upload to
   `https://<bmc>/redfish/v1/UpdateService/update-multipart` with
   exactly the same body shape as the canonical curl one-liner
   (`UpdateParameters={}` + `UpdateFile=@...fwpkg`, both
   `application/octet-stream`).
5. Follow the `Location` header to the Task and poll every 5s,
   logging `PercentComplete` until the task ends.
6. Exit non-zero if the task ends in any state other than `Completed`
   with status `OK`; the BMC's Redfish `Messages` are dumped to the
   log so you know why.

## Notes / safety

- TLS verification against the BMC is **disabled** (lab self-signed
  certs); a one-time warning is logged. Verification against
  Artifactory and the ISO mirror is **on**.
- BMC credentials live only in `benches.yaml`; the Artifactory token
  comes from `--artifactory-token` or the `ARTIFACTORY_*` env vars.
  Neither is ever logged.
- The downloader skips re-downloading files that are already present
  with the right size (and sha256 if known), so re-running the script
  is cheap.
- `--verbose` enables DEBUG logging, including Redfish payloads, which
  is useful when something goes sideways.

## Out of scope

- Triggering the OS install on the BF4 (BMC virtual-media mount + reboot)
  is intentionally **not** automated — do it manually using the printed
  HTTP URL.
- Multi-bench / parallel firmware updates — one `--bf4` per invocation.
  Loop in shell if you need several.
