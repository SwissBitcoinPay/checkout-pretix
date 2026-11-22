"""A PostgreSQL server for the tests, without Docker nor root: the tests of PostgreSQL's row locks
(marker `postgresql`) need one, and the CI starts it so on its GitHub-hosted runner, as a
developer can on any Linux x86_64 machine.

    python scripts/postgres.py start <directory>
        Downloads PostgreSQL's binaries for Linux x86_64 (zonky's embedded-postgres build, from
        Maven Central or Google's mirror of it, pinned by its SHA-256) into <directory>, makes a
        database cluster there and starts it, listening on a Unix socket in <directory> alone
        (no TCP port: nothing to collide with on a shared host); then writes
        <directory>/pretix.cfg, the pretix configuration file whose database is that server,
        and prints its path: PRETIX_CONFIG_FILE=<that path> python -m pytest tests (pretix's
        test settings run the migrations when GITHUB_WORKFLOW is set).
    python scripts/postgres.py stop <directory>
        Stops it, if it runs.

Its user is `pretix`, trusted without a password: only the user who started the server can
reach its socket.
"""

import hashlib
import io
import shlex
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import NoReturn

VERSION = "16.15.0"
PATH = (
    "io/zonky/test/postgres/embedded-postgres-binaries-linux-amd64/"
    f"{VERSION}/embedded-postgres-binaries-linux-amd64-{VERSION}.jar"
)
# Maven Central, then Google's mirror of it: one file, whichever serves it, never another.
SOURCES = (
    "https://repo1.maven.org/maven2/",
    "https://maven-central.storage-download.googleapis.com/maven2/",
)
# Of that file, as Maven Central publishes it (with its SHA-1 and SHA-512, which agree).
SHA256 = "653abc065c682b85d3da50168fb95dc524bd85426cdec9ce3695a550ef431df2"
ARCHIVE = "postgres-linux-x86_64.txz"
USER = "pretix"
# A Unix socket's path is at most 107 bytes.
SOCKET_PATH_MAX = 107


def fail(message: str) -> NoReturn:
    sys.exit(f"postgres: {message}")


def paths(directory: Path) -> tuple[Path, Path, Path]:
    """The binaries, the cluster, the socket's directory."""
    return directory / "pg", directory / "data", directory / "socket"


def download() -> bytes:
    """The pinned file, from the first source that serves it."""
    failures = []
    for source in SOURCES:
        try:
            with urllib.request.urlopen(source + PATH, timeout=120) as response:
                data: bytes = response.read()
        except OSError as e:
            failures.append(f"{source}: {e}")
            continue
        if hashlib.sha256(data).hexdigest() == SHA256:
            return data
        failures.append(f"{source}: not the file pinned (SHA-256)")
    fail("PostgreSQL's binaries could not be downloaded: " + "; ".join(failures))


def start(directory: Path) -> None:
    directory = directory.resolve()
    binaries, data, socket = paths(directory)
    if len(str(socket / ".s.PGSQL.5432").encode()) > SOCKET_PATH_MAX:
        fail(f"{directory}: too long a path for a Unix socket")
    if data.exists():
        fail(f"{data} exists: stop that server and remove {directory} first")
    jar = download()
    with zipfile.ZipFile(io.BytesIO(jar)) as archive:
        packed = archive.read(ARCHIVE)
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:xz") as tar:
        tar.extractall(binaries, filter="tar")
    socket.mkdir(mode=0o700, parents=True)
    initdb = [str(binaries / "bin" / "initdb"), "-D", str(data), "-U", USER]
    initdb += ["--auth=trust", "-E", "UTF8", "--locale=C"]
    subprocess.run(initdb, check=True, stdout=subprocess.DEVNULL)
    options = "-c listen_addresses='' -c unix_socket_permissions=0700"
    options += f" -k {shlex.quote(str(socket))}"
    pg_ctl = [str(binaries / "bin" / "pg_ctl"), "-D", str(data)]
    pg_ctl += ["-l", str(directory / "postgres.log"), "-o", options, "-w", "start"]
    subprocess.run(pg_ctl, check=True, stdout=subprocess.DEVNULL)
    config = directory / "pretix.cfg"
    config.write_text(
        f"[database]\nbackend=postgresql\nname=pretix\nuser={USER}\nhost={socket}\n",
        encoding="utf-8",
    )
    print(config)


def stop(directory: Path) -> None:
    binaries, data, _ = paths(directory.resolve())
    if not (data / "postmaster.pid").exists():
        return
    pg_ctl = [
        str(binaries / "bin" / "pg_ctl"),
        "-D",
        str(data),
        "-m",
        "fast",
        "-w",
        "stop",
    ]
    subprocess.run(pg_ctl, check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("start", "stop"):
        sys.exit(__doc__)
    (start if sys.argv[1] == "start" else stop)(Path(sys.argv[2]))
