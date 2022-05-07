import os
import re
import warnings
from datetime import date
from datetime import datetime
from os.path import isfile
from os.path import join
from os.path import samefile
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import TYPE_CHECKING
from typing import Union

from . import _types as _t
from .config import Configuration
from .scm_workdir import Workdir
from .utils import data_from_mime
from .utils import do_ex
from .utils import require_command
from .utils import trace
from .version import meta
from .version import ScmVersion
from .version import tags_to_versions

if TYPE_CHECKING:
    from setuptools_scm.hg_git import GitWorkdirHgClient

REF_TAG_RE = re.compile(r"(?<=\btag: )([^,]+)\b")
DESCRIBE_UNSUPPORTED = "%(describe"

# If testing command in shell make sure to quote the match argument like
# '*[0-9]*' as it will expand before being sent to git if there are any matching
# files in current directory.
DEFAULT_DESCRIBE = [
    "git",
    "describe",
    "--dirty",
    "--tags",
    "--long",
    "--match",
    "*[0-9]*",
]


class GitWorkdir(Workdir):
    """experimental, may change at any time"""

    COMMAND = "git"

    @classmethod
    def from_potential_worktree(cls, wd: _t.PathT) -> Optional["GitWorkdir"]:
        require_command(cls.COMMAND)
        wd = os.path.abspath(wd)
        real_wd, _, ret = do_ex("git rev-parse --show-prefix", wd)
        real_wd = real_wd[:-1]  # remove the trailing pathsep
        if ret:
            return None
        if not real_wd:
            real_wd = wd
        else:
            assert wd.replace("\\", "/").endswith(real_wd)
            # In windows wd contains ``\`` which should be replaced by ``/``
            # for this assertion to work.  Length of string isn't changed by replace
            # ``\\`` is just and escape for `\`
            real_wd = wd[: -len(real_wd)]
        trace("real root", real_wd)
        if not samefile(real_wd, wd):
            return None

        return cls(real_wd)

    def is_dirty(self) -> bool:
        out, _, _ = self.do_ex("git status --porcelain --untracked-files=no")
        return bool(out)

    def get_branch(self) -> Optional[str]:
        branch, err, ret = self.do_ex("git rev-parse --abbrev-ref HEAD")
        if ret:
            trace("branch err", branch, err, ret)
            branch, err, ret = self.do_ex("git symbolic-ref --short HEAD")
            if ret:
                trace("branch err (symbolic-ref)", branch, err, ret)
                return None
        return branch

    def get_head_date(self) -> Optional[date]:
        timestamp, err, ret = self.do_ex(
            "git -c log.showSignature=false log -n 1 HEAD --format=%cI"
        )
        if ret:
            trace("timestamp err", timestamp, err, ret)
            return None
        # TODO, when dropping python3.6 use fromiso
        date_part = timestamp.split("T")[0]
        if "%c" in date_part:
            trace("git too old -> timestamp is ", timestamp)
            return None
        return datetime.strptime(date_part, r"%Y-%m-%d").date()

    def is_shallow(self) -> bool:
        return isfile(join(self.path, ".git/shallow"))

    def fetch_shallow(self) -> None:
        self.do_ex("git fetch --unshallow")

    def node(self) -> Optional[str]:
        node, _, ret = self.do_ex("git rev-parse --verify --quiet HEAD")
        if not ret:
            return node[:7]
        else:
            return None

    def count_all_nodes(self) -> int:
        revs, _, _ = self.do_ex("git rev-list HEAD")
        return revs.count("\n") + 1

    def default_describe(self) -> _t.CmdResult:
        return self.do_ex(DEFAULT_DESCRIBE)


def warn_on_shallow(wd: GitWorkdir) -> None:
    """experimental, may change at any time"""
    if wd.is_shallow():
        warnings.warn(f'"{wd.path}" is shallow and may cause errors')


def fetch_on_shallow(wd: GitWorkdir) -> None:
    """experimental, may change at any time"""
    if wd.is_shallow():
        warnings.warn(f'"{wd.path}" was shallow, git fetch was used to rectify')
        wd.fetch_shallow()


def fail_on_shallow(wd: GitWorkdir) -> None:
    """experimental, may change at any time"""
    if wd.is_shallow():
        raise ValueError(
            f'{wd.path} is shallow, please correct with "git fetch --unshallow"'
        )


def get_working_directory(config: Configuration) -> Optional[GitWorkdir]:
    """
    Return the working directory (``GitWorkdir``).
    """

    if config.parent:
        return GitWorkdir.from_potential_worktree(config.parent)

    if config.search_parent_directories:
        return search_parent(config.absolute_root)

    return GitWorkdir.from_potential_worktree(config.absolute_root)


def parse(
    root: str,
    describe_command: Optional[Union[str, List[str]]] = None,
    pre_parse: Callable[[GitWorkdir], None] = warn_on_shallow,
    config: Optional[Configuration] = None,
) -> Optional[ScmVersion]:
    """
    :param pre_parse: experimental pre_parse action, may change at any time
    """
    if not config:
        config = Configuration(root=root)

    wd = get_working_directory(config)
    if wd:
        return _git_parse_inner(
            config, wd, describe_command=describe_command, pre_parse=pre_parse
        )
    else:
        return None


def _git_parse_inner(
    config: Configuration,
    wd: Union[GitWorkdir, "GitWorkdirHgClient"],
    pre_parse: Optional[
        Callable[[Union[GitWorkdir, "GitWorkdirHgClient"]], None]
    ] = None,
    describe_command: Optional[_t.CMD_TYPE] = None,
) -> ScmVersion:
    if pre_parse:
        pre_parse(wd)

    if config.git_describe_command is not None:
        describe_command = config.git_describe_command

    if describe_command is not None:
        out, _, ret = wd.do_ex(describe_command)
    else:
        out, _, ret = wd.default_describe()
    distance: Optional[int]
    node: Optional[str]
    if ret == 0:
        tag, distance, node, dirty = _git_parse_describe(out)
        if distance == 0 and not dirty:
            distance = None
    else:
        # If 'git git_describe_command' failed, try to get the information otherwise.
        tag = "0.0"
        node = wd.node()
        if node is None:
            distance = 0
        else:
            distance = wd.count_all_nodes()
            node = "g" + node
        dirty = wd.is_dirty()

    branch = wd.get_branch()
    node_date = wd.get_head_date() or date.today()

    return meta(
        tag,
        branch=branch,
        node=node,
        node_date=node_date,
        distance=distance,
        dirty=dirty,
        config=config,
    )


def _git_parse_describe(describe_output: str) -> Tuple[str, int, str, bool]:
    # 'describe_output' looks e.g. like 'v1.5.0-0-g4060507' or
    # 'v1.15.1rc1-37-g9bd1298-dirty'.

    if describe_output.endswith("-dirty"):
        dirty = True
        describe_output = describe_output[:-6]
    else:
        dirty = False

    tag, number, node = describe_output.rsplit("-", 2)
    return tag, int(number), node, dirty


def search_parent(dirname: _t.PathT) -> Optional[GitWorkdir]:
    """
    Walk up the path to find the `.git` directory.
    :param dirname: Directory from which to start searching.
    """

    # Code based on:
    # https://github.com/gitpython-developers/GitPython/blob/main/git/repo/base.py

    curpath = os.path.abspath(dirname)

    while curpath:

        try:
            wd = GitWorkdir.from_potential_worktree(curpath)
        except Exception:
            wd = None

        if wd is not None:
            return wd

        curpath, tail = os.path.split(curpath)

        if not tail:
            return None
    return None


def archival_to_version(
    data: Dict[str, str], config: Optional[Configuration] = None
) -> ScmVersion:
    trace("data", data)
    archival_describe = data.get("describe-name", DESCRIBE_UNSUPPORTED)
    if DESCRIBE_UNSUPPORTED in archival_describe:
        warnings.warn("git archive did not support describe output")
    else:
        tag, number, node, _ = _git_parse_describe(archival_describe)
        return meta(
            tag,
            config=config,
            distance=None if number == 0 else number,
            node=node,
        )
    versions = tags_to_versions(REF_TAG_RE.findall(data.get("ref-names", "")))
    if versions:
        return meta(versions[0], config=config)
    else:
        return meta("0.0", node=data.get("node"), config=config)


def parse_archival(
    root: _t.PathT, config: Optional[Configuration] = None
) -> Optional[ScmVersion]:
    archival = os.path.join(root, ".git_archival.txt")
    data = data_from_mime(archival)
    return archival_to_version(data, config=config)
