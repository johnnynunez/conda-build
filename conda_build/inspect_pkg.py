# Copyright (C) 2014 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from functools import lru_cache
from itertools import groupby
from operator import itemgetter
from os.path import abspath, basename, dirname, exists, join
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable, Literal

from conda.api import Solver
from conda.core.index import get_index
from conda.core.prefix_data import PrefixData
from conda.models.dist import Dist
from conda.models.records import PrefixRecord
from conda.resolve import MatchSpec

from . import conda_interface
from .conda_interface import (
    linked_data,
    specs_from_args,
)
from .deprecations import deprecated
from .os_utils.ldd import (
    get_linkages,
    get_package_obj_files,
    get_untracked_obj_files,
)
from .os_utils.liefldd import codefile_class, machofile
from .os_utils.macho import get_rpaths, human_filetype
from .utils import (
    comma_join,
    ensure_list,
    get_logger,
    on_linux,
    on_mac,
    on_win,
    package_has_file,
)

log = get_logger(__name__)


@deprecated("3.28.0", "24.1.0")
@lru_cache(maxsize=None)
def dist_files(prefix: str | os.PathLike | Path, dist: Dist) -> set[str]:
    if (prec := PrefixData(str(prefix)).get(dist.name, None)) is None:
        return set()
    elif MatchSpec(dist).match(prec):
        return set(prec["files"])
    else:
        return set()


@deprecated.argument("3.28.0", "24.1.0", "avoid_canonical_channel_name")
def which_package(
    path: str | os.PathLike | Path,
    prefix: str | os.PathLike | Path,
) -> Iterable[PrefixRecord]:
    """Detect which package(s) a path belongs to.

    Given the path (of a (presumably) conda installed file) iterate over
    the conda packages the file came from.  Usually the iteration yields
    only one package.

    We use lstat since a symlink doesn't clobber the file it points to.
    """
    prefix = Path(prefix)

    # historically, path was relative to prefix, just to be safe we append to prefix
    # get lstat before calling _file_package_mapping in case path doesn't exist
    try:
        lstat = (prefix / path).lstat()
    except FileNotFoundError:
        # FileNotFoundError: path doesn't exist
        return
    else:
        yield from _file_package_mapping(prefix).get(lstat, ())


@lru_cache(maxsize=None)
def _file_package_mapping(prefix: Path) -> dict[os.stat_result, set[PrefixRecord]]:
    """Map paths to package records.

    We use lstat since a symlink doesn't clobber the file it points to.
    """
    mapping: dict[os.stat_result, set[PrefixRecord]] = {}
    for prec in PrefixData(str(prefix)).iter_records():
        for file in prec["files"]:
            # packages are capable of removing files installed by other dependencies from
            # the build prefix, in those cases lstat will fail, while which_package wont
            # return the correct package(s) in such a condition we choose to not worry about
            # it since this file to package lookup exists primarily to detect clobbering
            try:
                lstat = (prefix / file).lstat()
            except FileNotFoundError:
                # FileNotFoundError: path doesn't exist
                continue
            else:
                mapping.setdefault(lstat, set()).add(prec)
    return mapping


def print_object_info(info, key):
    output_string = ""
    for header, group in groupby(sorted(info, key=itemgetter(key)), itemgetter(key)):
        output_string += header + "\n"
        for f_info in sorted(group, key=itemgetter("filename")):
            for data in sorted(f_info):
                if data == key:
                    continue
                if f_info[data] is None:
                    continue
                output_string += f"  {data}: {f_info[data]}\n"
            if len([i for i in f_info if f_info[i] is not None and i != key]) > 1:
                output_string += "\n"
        output_string += "\n"
    return output_string


class _untracked_package:
    def __str__(self):
        return "<untracked>"


untracked_package = _untracked_package()


@deprecated.argument("24.1.0", "24.3.0", "platform", rename="subdir")
@deprecated.argument("24.1.0", "24.3.0", "prepend")
@deprecated.argument("24.1.0", "24.3.0", "minimal_hint")
def check_install(
    packages: Iterable[str],
    subdir: str | None = None,
    channel_urls: Iterable[str] = (),
) -> None:
    with TemporaryDirectory() as prefix:
        Solver(
            prefix,
            channel_urls,
            [subdir or conda_interface.subdir],
            specs_from_args(packages),
        ).solve_for_transaction(ignore_pinned=True).print_transaction_summary()


def print_linkages(
    depmap: dict[
        PrefixRecord | Literal["not found" | "system" | "untracked"],
        list[tuple[str, str, str]],
    ],
    show_files: bool = False,
) -> str:
    # print system, not found, and untracked last
    sort_order = {
        # PrefixRecord: (0, PrefixRecord.name),
        "system": (1, "system"),
        "not found": (2, "not found"),
        "untracked": (3, "untracked"),
        # str: (4, str),
    }

    output_string = ""
    for prec, links in sorted(
        depmap.items(),
        key=(
            lambda key: (0, key[0].name)
            if isinstance(key[0], PrefixRecord)
            else sort_order.get(key[0], (4, key[0]))
        ),
    ):
        output_string += "%s:\n" % prec
        if show_files:
            for lib, path, binary in sorted(links):
                output_string += f"    {lib} ({path}) from {binary}\n"
        else:
            for lib, path in sorted(set(map(itemgetter(0, 1), links))):
                output_string += f"    {lib} ({path})\n"
        output_string += "\n"
    return output_string


def replace_path(binary, path, prefix):
    if on_linux:
        return abspath(path)
    elif on_mac:
        if path == basename(binary):
            return abspath(join(prefix, binary))
        if "@rpath" in path:
            rpaths = get_rpaths(join(prefix, binary))
            if not rpaths:
                return "NO LC_RPATH FOUND"
            else:
                for rpath in rpaths:
                    path1 = path.replace("@rpath", rpath)
                    path1 = path1.replace("@loader_path", join(prefix, dirname(binary)))
                    if exists(abspath(join(prefix, path1))):
                        path = path1
                        break
                else:
                    return "not found"
        path = path.replace("@loader_path", join(prefix, dirname(binary)))
        if path.startswith("/"):
            return abspath(path)
        return "not found"


def test_installable(channel: str = "defaults") -> bool:
    success = True
    for subdir in ["osx-64", "linux-32", "linux-64", "win-32", "win-64"]:
        log.info("######## Testing subdir %s ########", subdir)
        for prec in get_index(channel_urls=[channel], prepend=False, platform=subdir):
            name = prec["name"]
            if name in {"conda", "conda-build"}:
                # conda can only be installed in the root environment
                continue
            elif name.endswith("@"):
                # this is a 'virtual' feature record that conda adds to the index for the solver
                # and should be ignored here
                continue

            version = prec["version"]
            log.info("Testing %s=%s", name, version)

            try:
                check_install(
                    [f"{name}={version}"],
                    channel_urls=[channel],
                    prepend=False,
                    subdir=subdir,
                )
            except Exception as err:
                success = False
                log.error(
                    "[%s/%s::%s=%s] %s",
                    channel,
                    subdir,
                    name,
                    version,
                    repr(err),
                )
    return success


@deprecated("3.28.0", "24.1.0")
def _installed(prefix: str | os.PathLike | Path) -> dict[str, Dist]:
    return {dist.name: dist for dist in linked_data(str(prefix))}


def _underlined_text(text):
    return str(text) + "\n" + "-" * len(str(text)) + "\n\n"


def inspect_linkages(
    packages: Iterable[str | _untracked_package],
    prefix: str | os.PathLike | Path = sys.prefix,
    untracked: bool = False,
    all_packages: bool = False,
    show_files: bool = False,
    groupby: Literal["package" | "dependency"] = "package",
    sysroot="",
):
    if not packages and not untracked and not all_packages:
        sys.exit("At least one package or --untracked or --all must be provided")
    elif on_win:
        sys.exit("Error: conda inspect linkages is only implemented in Linux and OS X")

    prefix = Path(prefix)
    installed = {prec.name: prec for prec in PrefixData(str(prefix)).iter_records()}

    if all_packages:
        packages = sorted(installed.keys())
    packages = ensure_list(packages)
    if untracked:
        packages.append(untracked_package)

    pkgmap: dict[str | _untracked_package, dict[str, list]] = {}
    for name in packages:
        if name == untracked_package:
            obj_files = get_untracked_obj_files(prefix)
        elif name not in installed:
            sys.exit(f"Package {name} is not installed in {prefix}")
        else:
            obj_files = get_package_obj_files(installed[name], prefix)

        linkages = get_linkages(obj_files, prefix, sysroot)
        pkgmap[name] = depmap = defaultdict(list)
        for binary, paths in linkages.items():
            for lib, path in paths:
                path = (
                    replace_path(binary, path, prefix)
                    if path not in {"", "not found"}
                    else path
                )
                try:
                    relative = str(Path(path).relative_to(prefix))
                except ValueError:
                    # ValueError: path is not relative to prefix
                    relative = None
                if relative:
                    precs = list(which_package(relative, prefix))
                    if len(precs) > 1:
                        get_logger(__name__).warn(
                            "Warning: %s comes from multiple packages: %s",
                            path,
                            comma_join(map(str, precs)),
                        )
                    elif not precs:
                        if exists(path):
                            depmap["untracked"].append((lib, relative, binary))
                        else:
                            depmap["not found"].append((lib, relative, binary))
                    for prec in precs:
                        depmap[prec].append((lib, relative, binary))
                elif path == "not found":
                    depmap["not found"].append((lib, path, binary))
                else:
                    depmap["system"].append((lib, path, binary))

    output_string = ""
    if groupby == "package":
        for pkg in packages:
            output_string += _underlined_text(pkg)
            output_string += print_linkages(pkgmap[pkg], show_files=show_files)

    elif groupby == "dependency":
        # {pkg: {dep: [files]}} -> {dep: {pkg: [files]}}
        inverted_map = defaultdict(lambda: defaultdict(list))
        for pkg in pkgmap:
            for dep in pkgmap[pkg]:
                if pkgmap[pkg][dep]:
                    inverted_map[dep][pkg] = pkgmap[pkg][dep]

        # print system and not found last
        k = sorted(set(inverted_map.keys()) - {"system", "not found"})
        for dep in k + ["system", "not found"]:
            output_string += _underlined_text(dep)
            output_string += print_linkages(inverted_map[dep], show_files=show_files)

    else:
        raise ValueError("Unrecognized groupby: %s" % groupby)
    if hasattr(output_string, "decode"):
        output_string = output_string.decode("utf-8")
    return output_string


def inspect_objects(
    packages: Iterable[str],
    prefix: str | os.PathLike | Path = sys.prefix,
    groupby: str = "package",
):
    if not on_mac:
        sys.exit("Error: conda inspect objects is only implemented in OS X")

    prefix = Path(prefix)
    installed = {prec.name: prec for prec in PrefixData(str(prefix)).iter_records()}

    output_string = ""
    for name in ensure_list(packages):
        if name == untracked_package:
            obj_files = get_untracked_obj_files(prefix)
        elif name not in installed:
            raise ValueError(f"Package {name} is not installed in {prefix}")
        else:
            obj_files = get_package_obj_files(installed[name], prefix)

        output_string += _underlined_text(name)

        info = []
        for f in obj_files:
            path = join(prefix, f)
            codefile = codefile_class(path)
            if codefile == machofile:
                info.append(
                    {
                        "filetype": human_filetype(path, None),
                        "rpath": ":".join(get_rpaths(path)),
                        "filename": f,
                    }
                )

        output_string += print_object_info(info, groupby)
    if hasattr(output_string, "decode"):
        output_string = output_string.decode("utf-8")
    return output_string


def get_hash_input(packages):
    hash_inputs = {}
    for pkg in ensure_list(packages):
        pkgname = os.path.basename(pkg)
        hash_inputs[pkgname] = {}
        hash_input = package_has_file(pkg, "info/hash_input.json")
        if hash_input:
            hash_inputs[pkgname]["recipe"] = json.loads(hash_input)
        else:
            hash_inputs[pkgname] = "<no hash_input.json in file>"

    return hash_inputs
