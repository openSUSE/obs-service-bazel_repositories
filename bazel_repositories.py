#!/usr/bin/python3
"""\
OBS Source Service to download dependencies of projects using Bazel.
"""

import argparse
import codecs
import ctypes
import fcntl
import functools
import glob
import hashlib
import logging
import multiprocessing
from multiprocessing import connection as m_connection
import os
import pathlib
import re
import shutil
import socket
import subprocess
import tarfile
import threading
import typing
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
import uuid


app_name = "obs-service-bazel_repositories"
description = __doc__

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(app_name)

libc = ctypes.cdll.LoadLibrary("libc.so.6")

# Constants defined in the Linux kernel headers.
# uapi/linux/if.h
IFF_UP = 1 << 0
IFF_RUNNING = 1 << 6
# uapi/linux/sched.h
CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000
# uapi/linux/sockios.h
SIOCSIFFLAGS = 0x8914

URL_PATTERN = re.compile(r"(?a)https?://[-_@.&/+\w]+")
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
SHA1_PATTERN = re.compile(r"\w{40}")

BUF_SIZE = 65536
CACHEDIR = "BAZEL_CACHE/content_addressable/sha256"
AUTOGEN_HEADER = "# AUTOGENERATED BY obs-service-bazel_repositories\n"
AUTOGEN_FOOTER = "# END obs-service-bazel_repositories\n"


class Ifreq(ctypes.Structure):
    """ifreq structure from sys/ioctl.h"""
    _fields_ = [
        ("ifr_name", ctypes.c_char * 16),
        ("ifr_flags", ctypes.c_short),
    ]


def find_spec() -> str:
    """Find a spec file and return its name."""
    return glob.glob("*.spec")[0]


def apply_patch() -> int:
    """Apply the current patch from the current series."""
    p = subprocess.Popen(["quilt", "push"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    for line in iter(p.stdout.readline, ""):
        line_s = line.strip()
        log.debug(f"quilt push: {line_s}")
    p.stdout.close()
    return p.wait()


def quilt(outdir: str) -> str:
    """Iniitialize a source tree, apply patches and initialite a git
    repository. Returns the name of directory containing the source tree.
    """
    subdirs = [f.path for f in os.scandir(".") if f.is_dir()
               and not f.path.startswith("./.")
               and not f.path.endswith(outdir)]
    log.debug(f"subdirs: {subdirs}")
    for subdir in subdirs:
        shutil.rmtree(subdir, ignore_errors=True)

    p = subprocess.Popen(["quilt", "setup", "-v", find_spec()],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    root_dir = None
    stdout = ""
    for line in iter(p.stdout.readline, ""):
        line_s = line.strip()
        log.debug(f"quilt setup: {line_s}")
        stdout += line
        if line_s.startswith("+ cd"):
            root_dir = line.split()[-1]
    p.stdout.close()
    returncode = p.wait()

    if returncode != 0 or root_dir is None:
        log.error("quilt setup failed or was not able to create a source dir: "
                  f"{stdout}")
        log.error("Please ensure that %prep step in your spec does not fail "
                  f"and check if `quilt setup -v {find_spec()} runs without "
                  "errors.")

    os.chdir(root_dir)
    while True:
        # If all patches were already applied, quilt returns the code 2.
        if apply_patch() == 2:
            break

    try:
        os.unlink("patches")
    except (FileNotFoundError, IsADirectoryError):
        pass

    log.info("patches applied")

    shutil.rmtree(".pc", ignore_errors=True)
    subprocess.run(["git", "init"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "add", "."], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-m", "Dummy commit"],
                   check=True, stdout=subprocess.DEVNULL)
    os.chdir("..")

    return root_dir


def bazel_clean(root_dir: str):
    """Clean Bazel cache."""
    os.chdir(root_dir)
    subprocess.run(["bazel", "clean", "--expunge"], check=True)
    os.chdir("..")


def sub(conn: m_connection.Connection, root_dir: str,
        override_repository: str, target: str):
    """The main sub process function which runs Bazel inside a network
    namespace without Internet connectivity.
    """
    log.debug(f"sub target: {target}")
    os.chdir(root_dir)
    log.debug(f"root dir: {root_dir}")

    libc.unshare(CLONE_NEWUSER | CLONE_NEWNET)

    # Notify the parent process that namespaces got unshared and the lo
    # interface needs to be set up.
    conn.send(1)
    conn.recv()

    log.info("Running `bazel fetch` to check for dependencies")

    args = ["bazel", "fetch", "--repository_cache=../BAZEL_CACHE"]
    if override_repository:
        args += [f"--override_repository={dep}"
                 for dep in override_repository.split(",")]
    args.append(target)

    log.debug(f"args: {args}")

    p = subprocess.Popen(args, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,
                         universal_newlines=True)
    urls = []
    stdout = ""
    for line in iter(p.stdout.readline, ""):
        line_s = line.strip()
        log.debug(f"bazel fetch: {line_s}")
        urls += URL_PATTERN.findall(line_s)
        stdout += line

    p.stdout.close()
    returncode = p.wait()

    conn.send((returncode, urls,))
    conn.close()


def setns(pid: int):
    """Set the user and network namespace of the given process."""
    pid_s = str(pid)
    usernsfd = os.open(os.path.join("/proc", pid_s, "ns", "user"), os.O_RDONLY)
    netnsfd = os.open(os.path.join("/proc", pid_s, "ns", "net"), os.O_RDONLY)

    libc.setns(usernsfd, CLONE_NEWUSER)
    libc.setns(netnsfd, CLONE_NEWNET)

    os.close(usernsfd)
    os.close(netnsfd)


def lo_up():
    """Set up the loopback interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    ifr_lo = Ifreq()
    ifr_lo.ifr_name = b"lo"
    ifr_lo.ifr_flags = IFF_UP | IFF_RUNNING
    fcntl.ioctl(s, SIOCSIFFLAGS, ifr_lo)


def ns_lo_up(pid: int):
    """Set up the loopback interface in the network namespace of the given
    process.
    """
    setns(pid)
    lo_up()


def download(url: str, dst: str):
    """Download the given URL to the given destination."""
    log.info(f"Downloading {url}")
    urllib_request.urlretrieve(url, dst)
    log.info("Download finished")


def process_url(lock: threading.Lock, exclude: typing.List[str],
                url: str) -> typing.Tuple[str, str, bool]:
    """Download and store the archive from the given URL. Return the tuple with
    three values: URL, path of the archive in the cache and boolean value
    whether the dependency is excluded and should be removed later.
    """
    filename = pathlib.Path(urllib_parse.urlparse(url).path).parts[-1]
    # Sometimes the same dependency might be listed multiple times and have
    # the same filename. To avoid conflicts, append a random UUID.
    filename = f"{filename}-{uuid.uuid4()}"
    try:
        download(url, filename)
    except (urllib_error.HTTPError, urllib_error.URLError):
        log.warning(f"could not download {url}")
        return

    log.info(f"Checking sha256 sum of {url}")
    sha256sum = hashlib.sha256()
    with open(filename, "rb") as f:
        while True:
            buf = f.read(BUF_SIZE)
            if not buf:
                break
            sha256sum.update(buf)
    sha256sum_digest = sha256sum.hexdigest()

    # Sometimes we might have the same tarball listed in dependencies twice,
    # sometimes even under different names. In that case, that tarball is going
    # to be stored in the same `BAZEL_CACHE/<sha256sum>/file` location multiple
    # times. This lock ensures no conflicts in moving files to `BAZEL_CACHE`.
    lock.acquire()

    log.info(f"Storing {url}")
    os.makedirs(os.path.join(CACHEDIR, sha256sum_digest), exist_ok=True)
    cache_filename = os.path.join(CACHEDIR, sha256sum_digest, "file")
    shutil.move(filename, cache_filename)

    lock.release()

    excluded = False
    for e in exclude:
        if e in url:
            log.info(f"excluding dependency {url}")
            excluded = True

    return url, cache_filename, excluded


def process_urls(urls: typing.List[str], exclude: typing.List[str]) \
        -> typing.List[str]:
    """Download and store archives from the given list of URLs. Return the list
    of tuples with three values: URL, path of th archive in thee cache and
    boolean value whether the dependency is excluded and should be removed
    later.
    """
    # Remove duplicates
    urls = list(set(urls))

    pool = multiprocessing.Pool()
    manager = multiprocessing.Manager()
    lock = manager.Lock()
    func = functools.partial(process_url, lock, exclude)
    processed_urls = pool.imap(func, urls)
    processed_urls = [x for x in processed_urls if x is not None]

    return processed_urls


def compress_cache():
    """Compress the Bazel cache dir into a tarball."""
    with tarfile.open("vendor.tar.gz", "w:gz") as tar:
        tar.add(CACHEDIR)
    shutil.rmtree(CACHEDIR)


def clean_spec():
    """Clean the spec file from autogenerated content. This step is needed
    before running quilt to ensure that it will only use the main source
    tarball and not the vendor tarball produced by this service.
    """
    content = ""
    ignore = False
    log.info("cleaning spec file")
    with codecs.open(find_spec(), "r+", "utf-8") as f:
        for line in f:
            if ignore:
                if line.startswith(AUTOGEN_FOOTER):
                    ignore = False
                continue
            if line.startswith(AUTOGEN_HEADER):
                ignore = True
                continue
            content += line
        f.seek(0)
        f.write(content)
        f.truncate()
    log.info("spec file cleaned")


def deps_names_versions(urls: typing.List[str]) -> str:
    """Convert the list of urls to the list of project names and versions (if
    available.
    """
    deps = []

    for url in urls:
        url_parts = pathlib.Path(urllib_parse.urlparse(url).path).parts
        # The most of urls contain the project name in the second
        # part of the path, for example:
        # https://github.com/organization/project_name
        name = url_parts[2]
        # URLs from mirror.bazel.build contain the project name in
        # the third part of the path, for example:
        # https://mirror.bazel.build/github.com/organization/project_name
        if "mirror.bazel.build" in url:
            name = url_parts[3]
        name = name.lower()

        version = None
        version_m = VERSION_PATTERN.search(url)
        git_sha_m = SHA1_PATTERN.search(url)
        if version_m:
            version = version_m.group(0)
        elif git_sha_m:
            version = git_sha_m.group(0)

        deps.append((name, version,))

    deps = list(set(deps))
    deps.sort(key=lambda d: d[0])
    return deps


def spec_provides(urls: typing.List[str]) -> str:
    """Generate `Provide` tags for the given list of URLs."""
    content = ""
    content += AUTOGEN_HEADER

    deps = deps_names_versions(urls)
    for name, version in deps:
        content += "Provides:".ljust(16)
        content += f"bundled({name})"
        if version:
            content += f" = {version}"
        content += "\n"

    content += AUTOGEN_FOOTER

    return content


def spec_setup_vendor() -> str:
    """Add `%setup` macro for the vendor tarball."""
    content = ""
    content += AUTOGEN_HEADER
    content += "%setup -q -T -D -a 1\n"
    content += AUTOGEN_FOOTER

    return content


def spec_sources(urls: typing.List[str]) -> str:
    """Generate `Source´ tags for the given list of URLs."""
    content = ""
    content += AUTOGEN_HEADER
    content += "# vendor.tar.gz contains the following "
    content += "dependencies:\n"
    # List all fetched sources.
    for url in urls:
        content += f"# - {url}\n"
    content += "Source1:".ljust(16)
    content += "vendor.tar.gz\n"
    content += AUTOGEN_FOOTER

    return content


def update_spec(urls: typing.List[str]):
    """Update the spec file with URLs of fetched Bazel dependencies.
    """
    content = ""
    generate_provides = True
    generate_sources = True
    ignore = False
    log.info("updating spec file")
    with codecs.open(find_spec(), "r+", "utf-8") as f:
        for line in f:
            if ignore:
                if line.startswith(AUTOGEN_FOOTER):
                    ignore = False
                continue
            if line.startswith(AUTOGEN_HEADER):
                ignore = True
                continue
            if generate_provides and line.startswith("BuildRequires"):
                content += spec_provides(urls)
                generate_provides = False
            if line.startswith("%build"):
                content += spec_setup_vendor()
            content += line
            if generate_sources and line.startswith("Source"):
                content += spec_sources(urls)
                generate_sources = False
        f.seek(0)
        f.write(content)
        f.truncate()
    log.info("spec file updated")


def main():
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--exclude", default="")
    parser.add_argument("--outdir")
    parser.add_argument("--override-repository", default="")
    parser.add_argument("--target", default="//...")
    args = parser.parse_args()

    exclude = args.exclude.split(",") if args.exclude else []

    clean_spec()
    outdir_base = os.path.basename(args.outdir)
    root_dir = quilt(outdir_base)

    bazel_clean(root_dir)

    all_urls = []
    parent_conn, child_conn = multiprocessing.Pipe()
    while True:
        p = multiprocessing.Process(target=sub, args=(child_conn, root_dir,
                                                      args.override_repository,
                                                      args.target,))
        p.start()
        # Wait for the child process to unshare namespaces.
        parent_conn.recv()
        # Set up the lo interface in child's network namespace. It has to be
        # done in an another separate proccess.
        p_lo_up = multiprocessing.Process(target=ns_lo_up, args=(p.pid,))
        p_lo_up.start()
        p_lo_up.join()
        # Notify the child that the lo interface is ready.
        parent_conn.send(1)
        # Fetch the final output from the child.
        returncode, urls = parent_conn.recv()
        p.join()
        # If `bazel fetch` returned 0, it means that there are no more
        # dependencies to fetch.
        if returncode == 0:
            break
        log.debug(f"urls: {urls}")
        new_urls = process_urls(urls, exclude)
        all_urls += new_urls

    filtered_urls = []
    for url, cache_path, excluded in all_urls:
        if excluded:
            os.unlink(cache_path)
        else:
            filtered_urls.append(url)

    compress_cache()
    filtered_urls.sort()
    update_spec(filtered_urls)


if __name__ == "__main__":
    main()