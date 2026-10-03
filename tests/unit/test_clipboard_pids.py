import os
import subprocess
import sys

from local_stt.inject.clipboard import is_same_or_descendant, parent_pid


def test_parent_pid_of_this_process() -> None:
    assert parent_pid(os.getpid()) == os.getppid()
    assert parent_pid(2**22 + 12345) is None  # above pid_max: no such process


def test_descendants_but_not_ancestors() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert is_same_or_descendant(child.pid, os.getpid())
        assert is_same_or_descendant(child.pid, os.getppid())  # grandchild of the parent
        assert is_same_or_descendant(os.getpid(), os.getpid())
        assert not is_same_or_descendant(os.getpid(), child.pid)  # upwards never matches
        assert not is_same_or_descendant(2**22 + 12345, os.getpid())
    finally:
        child.kill()
        child.wait()
